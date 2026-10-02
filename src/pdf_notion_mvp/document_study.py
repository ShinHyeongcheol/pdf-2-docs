"""Read-only acceptance and native Markdown for source-reviewed document batches."""
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from .document_lesson import BatchDraft, DiagramEvidence, context_for, verify_draft,cloze_stem
from .lesson_generation import ContentReview, _regular
from .live_run import fingerprint
from .rag_files import load_review_files, read_json_input
from .study_bundle import sha
from .study_notion import encode_text, fence, toggle


def prose(text):
    # Native import auto-links names such as model.stream as web domains.
    parts=re.split(r'([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)',text)
    return ''.join('`'+p+'`' if i%2 else encode_text(p) for i,p in enumerate(parts))


def item_ids(draft, visual_notes=()):
    return [name for p in draft.pages for name in
            ([f'p{p.page}.note{i}' for i in range(1,len(p.notes)+1)]+
             [f'p{p.page}.exercise{i}' for i in range(1,len(p.exercises)+1)])]+[
                 'diagram:'+d.image_block_id for d in draft.diagrams]+[v['note_id'] for v in visual_notes]


def accept_batch(result_path, review_path, budget_path, *, revision_path=None):
    """No key, writes, retries or promotion of OCR. Failed drafts need a reviewed overlay."""
    result_path=Path(result_path);_regular(result_path);result_path=result_path.resolve()
    result=read_json_input(result_path,max_bytes=2_000_000)
    paths=[Path(p) for p in result['input_paths']]
    if [sha(p.read_bytes()) for p in paths]!=result['input_sha256']:
        raise ValueError('source input changed')
    files=load_review_files(*paths,[],result_path.parent/'unused.json')
    evidence=[DiagramEvidence.model_validate(d['evidence']) for d in result['context']['diagrams']]
    context,_=context_for(files,result['context']['pages'],evidence)
    if context!=result['context'] or fingerprint(context)!=result['context_digest']:
        raise ValueError('source context changed')
    ledger=Path(budget_path);_regular(ledger);ledger=ledger.resolve()
    if not ledger.is_file():raise ValueError('existing shared budget required')
    with sqlite3.connect('file:'+quote(str(ledger),safe='/')+'?mode=ro',uri=True) as db:
        receipt=db.execute('SELECT status,result_path,result_sha,request_count FROM reservations WHERE operation=?',
                           (result['operation_key'],)).fetchone()
    if (receipt is None or receipt[1:]!=(str(result_path),fingerprint(result),1) or
            receipt[0] not in {'completed','failed'} or result['request_count']!=1):
        raise ValueError('unchanged one-request budget receipt required')
    revision=read_json_input(Path(revision_path),max_bytes=2_000_000) if revision_path else None
    if revision:
        if (revision['original_result_digest']!=fingerprint(result) or
                Path(revision['original_result_path']).resolve()!=result_path or
                revision['additional_model_calls']!=0 or
                revision['reviewed_draft_digest']!=fingerprint(revision['reviewed_draft'])):
            raise ValueError('unchanged offline correction lineage required')
        material=revision;draft=BatchDraft.model_validate(revision['reviewed_draft'])
        visuals=revision.get('local_visual_notes',[])
    else:
        if receipt[0]!='completed' or result['status']!='ready_for_content_review' or result['errors']:
            raise ValueError('failed provider draft requires source-reviewed correction')
        material=result;draft=BatchDraft.model_validate(result['draft']);visuals=[]
    verify_draft(context,draft)
    ids=item_ids(draft,visuals)
    if len(set(ids))!=len(ids):raise ValueError('unique material item IDs required')
    review=ContentReview.model_validate(read_json_input(Path(review_path)))
    if (review.decision!='accepted' or review.review_kind!='independent_source_comparison' or
            review.result_digest!=fingerprint(material) or len(review.reviewed_ids)!=len(ids) or
            set(review.reviewed_ids)!=set(ids)):
        raise ValueError('complete independent source review required')
    images={b.block_id:b for b in files.source.document.blocks if b.kind=='image'}
    for v in visuals:
        image=images.get(v['source_image_block_id'])
        if (image is None or v['page'] not in context['pages'] or v['source']!=image.source.model_dump(mode='json') or
                v['source_image_sha256']!=image.sha256 or v['page']!=image.source.page or
                v['status']!='candidate_caption' or v['model_images_transmitted'] is not False or
                v['basis']!='independent_local_visual_observation' or not v['text'].strip()):
            raise ValueError('source-bound local visual candidate required')
    return dict(schema_version='1',status='source_reviewed_with_local_corrections' if revision else 'source_reviewed',
        acceptance_inputs=dict(result_path=str(result_path),review_path=str(Path(review_path).resolve()),
            budget_path=str(ledger),revision_path=str(Path(revision_path).resolve()) if revision_path else None),
        original_result_digest=fingerprint(result),accepted_material_digest=fingerprint(material),
        provider_result_status=result['status'],context=context,draft=draft.model_dump(mode='json'),
        local_visual_notes=visuals,exercise_origins=revision.get('exercise_origins',{}) if revision else {},
        review=review.model_dump(mode='json'),original_provider_preserved=True,
        full_ocr_semantics_verified=False,code_execution_verified=False,additional_model_calls=0)


def render_batch(material,source_url,*,diagram_urls=None):
    """Native source toggles use JSON to preserve OCR whitespace and adjacent units."""
    references=material.get('acceptance_inputs')
    if not references or accept_batch(**references)!=material:
        raise ValueError('accepted material changed; reaccept current source and review')
    from .notion_mcp import _uuid_from_url
    url=urlsplit(source_url)
    if url.scheme!='https' or url.hostname not in {'app.notion.com','www.notion.so','notion.so'} or url.query or url.fragment or url.username or url.password:
        raise ValueError('explicit native source page URL required')
    _uuid_from_url(source_url)
    context=material['context'];draft=BatchDraft.model_validate(material['draft'])
    units={u['unit_id']:u for u in context['units']};diagram_urls=diagram_urls or {}
    marker='pdf-notion-document-study:v1:'+material['accepted_material_digest']
    parts=[marker+':begin',
        'Gemini 초안을 독립적으로 원문과 대조하고 필요한 부분을 국소 수정했습니다. 전체 OCR 정확성·코드 실행·최신 SDK 동작은 확인하지 않았습니다.',
        '<mention-page url="'+source_url+'"/>',
        '출처의 미확정 OCR은 그대로 표시합니다. 문장이 여러 블록으로 나뉘면 아래 전사와 원본 페이지를 함께 확인하세요.']
    for p in draft.pages:
        parts+=['## 원본 p'+str(p.page)+' · '+prose(p.title),prose(p.uncertainty)]
        for i,n in enumerate(p.notes,1):
            parts += [prose(n.text),toggle('설명 근거 · 블록·페이지·좌표',fence(json.dumps(
                [units[x] for x in n.unit_ids],ensure_ascii=False,indent=2),'json'))]
        for v in material.get('local_visual_notes',[]):
            if v['page']==p.page:parts += [prose(v['text']),toggle('로컬 원본 이미지 관찰 후보 · 생성 모델 미전송',fence(json.dumps(v,ensure_ascii=False,indent=2),'json'))]
        for i,q in enumerate(p.exercises,1):
            key=f'p{p.page}.exercise{i}'
            origin=material.get('exercise_origins',{}).get(key,'Gemini 초안 · 독립 대조 및 국소 수정 여부는 검토 기록 참조')
            evidence=[units[j] for j in q.context_unit_ids]+[units[q.unit_id]] if q.context_unit_ids else units[q.unit_id]
            parts += ['### 빈칸 문제',encode_text(str(origin)),fence(cloze_stem(context,q)),
                toggle('정답과 해설',prose('정답: '+q.answer)+'\n'+prose(q.explanation)),
                toggle('문제 원문 · 블록·페이지·좌표',fence(json.dumps(evidence,ensure_ascii=False,indent=2),'json'))]
        for d in draft.diagrams:
            evidence=next(x for x in context['diagrams'] if x['evidence']['image_block_id']==d.image_block_id)
            if evidence['source']['page']!=p.page:continue
            if d.image_block_id in diagram_urls:
                value=diagram_urls[d.image_block_id]
                if not value.startswith('file-upload://'):raise ValueError('explicit host upload binding required')
                UUID(value[len('file-upload://'):])
                parts.append('![도표 원본 p'+str(p.page)+']('+value+')')
            parts.append(toggle('도표 관찰 설명 · 후보',prose(d.text)+'\n'+prose(d.uncertainty)+'\n'+fence(json.dumps(evidence,ensure_ascii=False,indent=2),'json')))
        parts.append(toggle('p'+str(p.page)+' 전체 전사 · 원문·교정 상태·절·좌표',fence(json.dumps(
            [u for u in context['units'] if u['source']['page']==p.page],ensure_ascii=False,separators=(',',':')),'json')))
    parts += [toggle('독립 자료 대조 기록',fence(json.dumps(material['review'],ensure_ascii=False,indent=2),'json')),marker+':end','## 사용자 메모','검토하면서 여기에 메모를 남겨 주세요.']
    content='\n\n'.join(parts)
    if len(content.encode())>150_000:raise ValueError('bounded native page required')
    return content
