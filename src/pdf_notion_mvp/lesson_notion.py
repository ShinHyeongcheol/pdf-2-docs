"""Prepare one reviewed model lesson for a connected Notion host; no network or keys."""
import argparse
import json
import os
import re
from pathlib import Path
from uuid import UUID

from .lesson_generation import write_reading_bundle, fingerprint,parse_draft
from .instructional_lesson import InstructionalDraft,render_teaching,render_practice,place_materials
from .notion_mcp import encode_text
from .notion_bridge import page_body,separate_memo
from .rag_files import load_review_files, read_json_input
from .study_bundle import build_bundle, private_destination
from .study_notion import (MARKER, StudyCheckpoint, canonical, complete_record, fence,
                          prepare_study, save_study_checkpoint, toggle)
from .study_session import _save


def render_generated(result, review):
    units={u['unit_id']:u for u in result['context']['units']}
    def evidence(claim):
        rows=[]
        for citation in claim['citations']:
            pages=sorted({r['source']['page'] for r in units[citation['unit_id']]['sources']})
            rows += [encode_text('출처: 원문 '+', '.join('p'+str(p) for p in pages)),
                     '> '+encode_text(citation['quote'])]
        return toggle('원문 근거','\n'.join(rows))
    parts=['## 학습 설명',encode_text('선택한 원자료를 바탕으로 원문과 대조한 학습 요약입니다.')]
    for topic in result['draft']['topics']:
        parts.append('### '+encode_text(topic['title']))
        for claim in topic['claims']:parts += [encode_text(claim['text']),evidence(claim)]
    parts.append('## 확인 문제')
    for index,q in enumerate(result['draft']['exercises'],1):
        parts += ['### 문제 '+str(index),encode_text(q['question']),toggle('정답과 해설',
            encode_text('정답: '+q['answer'])+'\n'+encode_text(q['explanation']['text'])+'\n'+evidence(q['explanation'])+
            ('\n'+toggle('문제의 원문 근거',encode_text('출처: 원문 '+', '.join('p'+str(p) for p in sorted({r['source']['page'] for r in units[q['unit_id']]['sources']})))+'\n'+'> '+encode_text(q['source_quote']))
             if q['source_quote'] not in [c['quote'] for c in q['explanation']['citations']] else ''))]
    notes=[encode_text(note) for note in review['notes']]
    if notes:parts.append(toggle('읽기 범위와 검토 안내','\n'.join(notes)))
    return '\n\n'.join(parts)


def render_table(text):
    rows=[]
    for line in text.splitlines():
        cells=[cell.strip() for cell in line.strip().strip('|').split('|')]
        if cells and all(re.fullmatch(r':?-{3,}:?',cell) for cell in cells):continue
        rows.append(cells)
    if not rows or len(rows[0])<2 or any(len(row)!=len(rows[0]) for row in rows):
        raise ValueError('reviewed rectangular table required')
    parts=['<table>']
    for row in rows:
        parts += ['<tr>',*['<td>'+encode_text(cell)+'</td>' for cell in row],'</tr>']
    return '\n'.join([*parts,'</table>'])


def render_sources(bundle, images, *, placed_pages=(), placed_fragments=()):
    parts=['## 원문 읽기',encode_text('아래 전사와 표·코드는 해당 원본 페이지와 함께 읽어주세요.')]
    fragments=bundle['fragments'];grouped={i for f in fragments for i in f['source_block_ids']}
    block_pages={entry['original']['block_id']:entry['original']['source']['page'] for entry in bundle['blocks']}
    for page in bundle['selected_source_pages']:
        binding=next(i for i in images if i.page==page)
        parts += ['### 원본 p'+str(page)]
        if page not in placed_pages:parts.append(f'![원본 p{page}](file-upload://{binding.file_upload_id})')
        else:parts.append('원본 이미지는 위 설명 옆에 배치했습니다.')
        prose=[]
        for entry in bundle['blocks']:
            block=entry['original']
            if block['source']['page']!=page or block['kind']=='image' or block['block_id'] in grouped:continue
            if block['kind']=='text':
                correction=entry['correction']
                text=entry['effective_text']
                # Preserve printed numbering and bullet glyphs: native lists rewrite them.
                prefix='> ' if re.match(r'^(?:\d+\.\s|[•●]\s)',text) else ''
                prose.append(prefix+encode_text(text))
                if correction and correction['proposed_text']!=block['text']:
                    label='원문 전사와 확정 교정' if correction['status']=='confirmed' else '원문 전사와 교정 후보'
                    prose.append(toggle(label,encode_text('원문 전사: '+block['text'])+'\n'+encode_text('교정: '+correction['proposed_text'])))
            elif block['kind']=='code':prose.append(fence(block['code'],block.get('language') or 'text'))
            elif block['kind']=='table':
                prose.append(render_table('\n'.join(' | '.join(row) for row in block['cells'])))
        if prose:parts.append(toggle('p'+str(page)+' 원문 전사','\n'.join(prose)))
        for fragment in fragments:
            if fragment['fragment_id'] in placed_fragments:continue
            pages=sorted({block_pages[i] for i in fragment['source_block_ids']})
            if page!=min(pages):continue
            status='원문과 대조한 자료' if fragment['status']=='confirmed' else '미확정 후보 · 원본 확인 필요'
            label=('코드' if fragment['kind']=='code' else '표' if fragment['kind']=='table' else '원문')+' · '+status
            content=(fence(fragment['text'],'python') if fragment['kind']=='code' else
                     render_table(fragment['text']) if fragment['kind']=='table' else encode_text(fragment['text']))
            parts += ['#### '+encode_text(label),encode_text('출처: '+', '.join('p'+str(p) for p in pages)),content]
    return '\n\n'.join(parts)


def render_instructional_notion(result,bundle,bindings):
    draft=parse_draft(result['draft'],'instructional_v1')
    owners=place_materials(draft,result['context'],bundle)
    placed_pages=set();placed_fragments=set()
    def materials(u):
        rows=[]
        if owners[u.teaching_id]:rows+=['### 원본 자료와 설명 맞춰 읽기',encode_text(u.material_reading.text)]
        for f,pages in owners[u.teaching_id]:
            rows+=['출처: PDF '+', '.join('p'+str(p) for p in sorted(pages))]
            if f['status']=='candidate':rows+=['미확정 코드 후보 · 원본에서 호출 흐름을 확인하세요. 실행 미검증.']
            else:
                rows += [
                   '원문과 대조한 코드 · 실행 미검증' if f['kind']=='code' else '원문과 대조한 설정 표',
                   fence(f['text'],'python') if f['kind']=='code' else render_table(f['text'])]
                placed_fragments.add(f['fragment_id'])
            for p in sorted(pages-placed_pages):
                i=next(i for i in bindings if i.page==p)
                rows.append(f'![원본 p{p}](file-upload://{i.file_upload_id})');placed_pages.add(p)
        return rows
    body=render_teaching(draft,result['context'],after_unit=materials)
    body+='\n\n'+toggle('원본과 전사 · 필요할 때 펼치기',render_sources(bundle,bindings,
                               placed_pages=placed_pages,placed_fragments=placed_fragments))
    return body+'\n\n'+render_practice(draft)


def confirm_lesson(packet, checkpoint, images):
    cp=StudyCheckpoint.model_validate(checkpoint.model_dump())
    if cp.page_id is None:raise ValueError('created page binding required before readback')
    body=page_body(packet,cp.page_id,cp.title,cp.hub_id)
    owned,_=separate_memo(body)
    if canonical(owned,images,native_code_payload=True)!=canonical(cp.expected_content,images):
        raise ValueError('readback mismatch; preserve pending state')
    return cp.model_copy(update={'status':'confirmed'})


def prepare_lesson(result_path, review_path, budget_path, question, reading_dir,
                   hub_id, hub_packet, images, checkpoint=None, remote_packet=None, *, title=None):
    result=read_json_input(Path(result_path),max_bytes=500000)
    if MARKER in json.dumps(result,ensure_ascii=False):raise ValueError('result contains reserved publication marker')
    paths=[Path(p) for p in result['input_paths']]
    files=load_review_files(*paths,[],Path(reading_dir)/'unused.json')
    # Revalidate current source, all review IDs and the original execution receipt.
    final,_=write_reading_bundle(Path(result_path),Path(review_path),files,result['context']['section_id'],
                                question,Path(reading_dir),budget_path=Path(budget_path))
    reviewed=read_json_input(final/'lesson.json',max_bytes=1000000)
    if MARKER in json.dumps(reviewed['content_review'],ensure_ascii=False):raise ValueError('review contains reserved publication marker')
    # Reuse the source-image binding and complete hub checks from the source bridge.
    _,source_cp,bindings=prepare_study(files,result['context']['section_id'],question,hub_id,hub_packet,images)
    bundle,_=build_bundle(files,result['context']['section_id'],question)
    if isinstance(parse_draft(result['draft']),InstructionalDraft):
        content=render_instructional_notion(result,bundle,bindings)
    else:
        content=render_generated(result,reviewed['content_review'])+'\n\n'+render_sources(bundle,bindings)
    key=fingerprint(['reviewed-model-notion-v2-readable',source_cp.operation_key,fingerprint(result),reviewed['content_review'],fingerprint(content)])
    canonical(content,bindings)
    complete_record(dict(metadata={'type':'page'},text='<page>\n<content>\n'+content+'\n</content>\n</page>'))
    title=title or bundle['title']+' · 학습 설명과 확인 문제'
    cp=StudyCheckpoint(operation_key=key,hub_id=UUID(str(hub_id)),title=title,expected_content=content)
    if checkpoint is not None:
        old=StudyCheckpoint.model_validate(checkpoint.model_dump())
        if (old.operation_key,old.hub_id,old.title,old.expected_content)!=(cp.operation_key,cp.hub_id,cp.title,cp.expected_content):
            raise ValueError('checkpoint/source/target mismatch')
        if remote_packet is None:raise ValueError('existing checkpoint requires remote readback; do not repeat create')
        return None,confirm_lesson(remote_packet,old,bindings),bindings,final
    action=dict(parent={'page_id':str(cp.hub_id)},pages=[dict(properties={'title':title},
        content=content+'\n\n## 사용자 메모\n학습 의견을 이 영역에 적어주세요.')])
    return action,cp,bindings,final


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    prepare=sub.add_parser('prepare',help='Validate reviewed result; save pending before emitting one create action')
    for name in ['result','content-review','budget-ledger','reading-dir','hub-packet','images','checkpoint','action-output']:
        prepare.add_argument('--'+name,type=Path,required=True)
    prepare.add_argument('--hub-id',required=True,type=UUID);prepare.add_argument('--question',required=True)
    prepare.add_argument('--remote-packet',type=Path)
    prepare.add_argument('--title')
    bind=sub.add_parser('bind',help='Bind the returned page ID without another create')
    bind.add_argument('--checkpoint',type=Path,required=True);bind.add_argument('--page-id',type=UUID,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='bind':
            cp=StudyCheckpoint.model_validate(read_json_input(args.checkpoint,max_bytes=1000000))
            save_study_checkpoint(args.checkpoint,cp.model_copy(update={'page_id':args.page_id}))
            os.chmod(args.checkpoint,0o600)
            packet=dict(status='page_bound',page_id=str(args.page_id),notion_requests=0)
        else:
            result=read_json_input(args.result,max_bytes=500000)
            protected=[args.result,args.content_review,args.budget_ledger,args.hub_packet,args.images,*map(Path,result['input_paths'])]
            for output in [args.checkpoint,args.action_output]:
                private_destination(output.parent,protected)
                if output.resolve() in {p.resolve() for p in protected}:raise ValueError('output aliases input')
            old=StudyCheckpoint.model_validate(read_json_input(args.checkpoint,max_bytes=1000000)) if args.checkpoint.exists() else None
            remote=read_json_input(args.remote_packet,max_bytes=1000000) if args.remote_packet else None
            action,cp,_,final=prepare_lesson(args.result,args.content_review,args.budget_ledger,args.question,args.reading_dir,
                args.hub_id,read_json_input(args.hub_packet,max_bytes=1000000),read_json_input(args.images),old,remote,title=args.title)
            save_study_checkpoint(args.checkpoint,cp,protected_paths=protected)
            os.chmod(args.checkpoint,0o600)
            if action is not None:_save(args.action_output,action)
            packet=dict(status='action_prepared' if action else 'confirmed',action_path=str(args.action_output) if action else None,
                checkpoint=str(args.checkpoint),page_id=str(cp.page_id) if cp.page_id else None,
                index_html=str(final/'index.html'),model_requests=0,key_reads=0,notion_requests=0)
        print(json.dumps(packet,ensure_ascii=False))
    except (ValueError,OSError,KeyError) as exc:
        parser.exit(1,json.dumps(dict(status='blocked',error_type=type(exc).__name__,
            reason='Validate inputs, independent review, receipt and checkpoint; never repeat an uncertain create.'),ensure_ascii=False)+'\n')


if __name__=='__main__':main()
