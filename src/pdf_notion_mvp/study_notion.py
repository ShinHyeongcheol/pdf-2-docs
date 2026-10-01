"""Host-MCP create/readback bridge for one reviewed section; no API client or keys."""
import json
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID
from typing import Literal

from .contracts import Contract
from .notion_mcp import _uuid_from_url, decode_text, encode_text
from .study_bundle import build_bundle, encode, sha

MARKER = 'pdf-notion-study:v1:'


class UploadedPageImage(Contract):
    page: int
    sha256: str
    file_upload_id: UUID
    filename: str


class StudyCheckpoint(Contract):
    operation_key: str
    hub_id: UUID
    title: str
    expected_content: str
    status: Literal['pending_readback', 'confirmed'] = 'pending_readback'
    page_id: UUID | None = None


def complete_record(packet):
    if packet.get('isError'): raise ValueError('MCP read failed')
    record = packet.get('structuredContent') or packet
    if 'metadata' not in record:
        records = [json.loads(c['text']) for c in packet.get('content',[]) if c.get('type')=='text']
        if len(records)!=1: raise ValueError('ambiguous MCP record')
        record = records[0]
    if (record.get('metadata',{}).get('type')!='page' or record.get('truncated') or
            record.get('unknown_block_count') or record.get('unknown_block_ids')):
        raise ValueError('complete native page read required')
    text = record['text']
    state='before_page';code_prefix=None;offset=0;start=end=None
    for line in text.splitlines(keepends=True):
        raw=line.rstrip('\n')
        if code_prefix is not None:
            if raw==code_prefix+'```':code_prefix=None
        elif state=='body' and re.fullmatch(r'\t*```[^`]*',raw):
            code_prefix=raw[:len(raw)-len(raw.lstrip('\t'))]
        elif re.match(r'\s*<unknown(?:\s|/?>)',raw):
            raise ValueError('unsupported native block')
        elif state=='before_page' and re.fullmatch(r'<page(?:\s[^>]*)?>',raw):
            state='page_header'
        elif raw=='<content>':
            if state!='page_header':raise ValueError('ambiguous content wrapper')
            state='body';start=offset+len(line)
        elif raw=='</content>':
            if state!='body':raise ValueError('invalid content closure')
            state='after_body';end=offset
        elif raw=='</page>':
            if state!='after_body':raise ValueError('invalid page closure')
            state='done'
        elif state in ('after_body','done') and raw.strip():
            raise ValueError('unexpected trailing wrapper content')
        offset+=len(line)
    if state!='done' or code_prefix is not None or start is None or end is None:
        raise ValueError('complete fenced page/content wrapper required')
    return record,text[start:end]


def fence(text, language='text'):
    if '```' in text or any(c in text for c in '\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'): raise ValueError('unsupported literal code delimiter or separator')
    return '```'+language+'\n'+text+'\n```'


def toggle(title, content):
    return '<details>\n<summary>'+encode_text(title)+'</summary>\n'+ '\n'.join('\t'+line for line in content.split('\n'))+'\n</details>'


def render_study(bundle, images, key):
    parts = [MARKER+key+':begin', '## 읽기와 검토 범위']
    parts += [encode_text(text) for text in bundle['disclosures']]
    parts += [encode_text(f"문서 전체 {bundle['full_source_pages']}쪽 / {bundle['full_source_blocks']}블록. 선택한 절 페이지 {bundle['selected_source_pages']}. 모델 생성·코드 실행·의미 정확성 검증 없음."),
              encode_text('절 목차 검토 상태: '+bundle['outline_review_status'])]
    for page in bundle['selected_source_pages']:
        image = next(i for i in images if i.page==page)
        parts += [f'## 원본 p{page}', f'![원본 p{page}](file-upload://{image.file_upload_id})',
                  '원본 이미지 SHA256: '+image.sha256, '### 전사와 교정']
        records=[]
        for entry in bundle['blocks']:
            block = entry['original']
            if block['source']['page']!=page: continue
            record = dict(block_id=block['block_id'],kind=block['kind'],source={k:block['source'][k] for k in ['page','bbox','method','confidence']})
            if block['kind']=='text':record['original_text']=block['text']
            elif block['kind']=='image':record['sha256']=block['sha256']
            else:record['original']=block
            if entry['correction']:
                record['correction']={k:entry['correction'][k] for k in ['correction_id','status','proposed_text','basis','reviewer','evidence_block_id','evidence_bbox']}
            records.append(record)
            if block['kind']=='text':
                c = entry['correction']
                status = {'confirmed':'확정 교정','candidate':'교정 후보 · 본문 미적용'}[c['status']] if c else '교정 없음'
                parts += [encode_text(f"{block['block_id']} · p{page} · {status}"), fence(entry['effective_text'])]
                if c and c['status']=='candidate':
                    parts.append(toggle('교정 후보 제안 · 본문 미적용',fence(c['proposed_text'])))
            elif block['kind']!='image':
                parts.append(toggle('원본 '+block['kind']+' · 의미 미검증',fence(json.dumps(block,ensure_ascii=False,indent=2),'json')))
        parts.append(toggle(f'p{page} 원본 전사·교정·블록·좌표 계보',fence(json.dumps(records,ensure_ascii=False,separators=(',',':')),'json')))
    parts.append('## 파생 코드·표·텍스트 검토')
    for fragment in bundle['fragments']:
        label = '확정 검토 기록' if fragment['status']=='confirmed' else '미확정 후보'
        body = fence(fragment['text'],'python' if fragment['kind']=='code' else 'text')
        body += '\n'+encode_text('검토 상태를 그대로 표시합니다. 실행·기술 정확성 미검증.')
        body += '\n'+fence(json.dumps(fragment,ensure_ascii=False,indent=2),'json')
        parts.append(toggle('파생 '+fragment['kind']+' · '+label,body))
    quiz = bundle['local_quiz']
    parts += ['## 로컬 규칙 문제',encode_text('로컬 빈칸 규칙 · 모델 생성 아님 · '+quiz['status'])]
    for item in quiz['questions']:
        q = item['question']
        parts += [fence(q['question']),toggle('정답과 근거 인용',fence('정답: '+q['answer']+'\n'+q['source_quote']+'\n'+q['explanation'])),
                  toggle('문제 출처·검토 계보',fence(json.dumps(item,ensure_ascii=False,indent=2),'json'))]
    answer=bundle['local_answer']
    parts += ['## 로컬 인용 답변',encode_text('키워드 검색 / 모의 인용 조립 · 실제 모델 답변 아님 · '+answer['status']),fence('질문: '+answer['question'])]
    if answer['draft']:
        parts += [fence(answer['draft']['answer']),toggle('인용 출처 · 의미 검수 아님',fence(json.dumps(answer['draft']['citations'],ensure_ascii=False,indent=2),'json'))]
    else: parts.append('확정 근거에서 인용 가능한 답변 없음')
    parts += [toggle('문서 버전·검토 식별자',fence(json.dumps({k:bundle[k] for k in ['document_id','version','source_digest','review_digest','section_id']},ensure_ascii=False,indent=2),'json')),MARKER+key+':end']
    content='\n\n'.join(parts)
    if len(content.encode())>150_000: raise ValueError('bounded MCP content limit exceeded')
    return content


def canonical(content, images, *, native_code_payload=False):
    """Preserve literal code; normalize connector spacing/escapes and signed image URLs."""
    output=[]; index=0; lines=content.splitlines()
    by_page={i.page:i for i in images}; seen=set()
    while index<len(lines):
        line=lines[index]
        opening=re.fullmatch(r'(\t*)```[^`]*',line)
        if opening:
            prefix=opening[1];body=[];index+=1
            while index<len(lines) and lines[index]!=prefix+'```':
                if not native_code_payload and not lines[index].startswith(prefix): raise ValueError('lost code nesting')
                body.append(lines[index] if native_code_payload else lines[index][len(prefix):]);index+=1
            if index==len(lines): raise ValueError('unclosed code block')
            language=line[len(prefix)+3:].strip().lower()
            if language=='plain text':language='text'
            output.append(['literal_code',prefix,language,'\n'.join(body)])
        else:
            image=re.fullmatch(r'!\[원본 p(\d+)\]\((.+)\)',line)
            if image:
                page=int(image[1]);binding=by_page.get(page)
                if binding is None or page in seen:raise ValueError('unknown or duplicate image')
                url=image[2];parts=urlsplit(url)
                if url.startswith('file-upload://'):
                    if UUID(url[len('file-upload://'):])!=binding.file_upload_id:raise ValueError('wrong upload')
                elif not (parts.scheme=='https' and (parts.hostname or '').endswith(('.amazonaws.com','.notion.so','.notion-static.com')) and unquote(Path(parts.path).name)==binding.filename):
                    raise ValueError('unexpected uploaded image reference')
                seen.add(page);output.append(['image',page,str(binding.file_upload_id)])
            elif line.strip(): output.append(['markdown',decode_text(line)])
        index+=1
    if seen!=set(by_page):raise ValueError('missing page images')
    return output


def prepare_study(files, section_id, question, hub_id, hub_packet, images, checkpoint=None, remote_packet=None):
    hub_id=UUID(str(hub_id));record,_=complete_record(hub_packet)
    if _uuid_from_url(record['url'])!=hub_id:raise ValueError('wrong designated hub')
    bundle,_=build_bundle(files,section_id,question)
    images=[UploadedPageImage.model_validate(i.model_dump() if hasattr(i,'model_dump') else i) for i in images]
    expected={}
    pages={p.number:p for p in files.source.document.pages}
    for number in bundle['selected_source_pages']:
        candidates=[b for b in files.source.document.blocks if b.kind=='image' and b.source.page==number]
        if files.source.kind=='ocr_ir':
            candidates=[b for b in candidates if b.source.method=='raster']
            if len(candidates)!=1:raise ValueError('one explicit full-page raster required')
            box=candidates[0].source.bbox;page=pages[number]
            if (box.x0,box.y0,box.x1,box.y1)!=(0,0,page.width,page.height):
                raise ValueError('raster must cover the entire source page')
        elif len(candidates)!=1 or candidates[0].source.method!='synthetic':
            raise ValueError('one authored synthetic image reference required')
        expected[number]=candidates[0].sha256

    if not expected or len(images)!=len(expected) or {i.page:i.sha256 for i in images}!=expected or len({i.page for i in images})!=len(images):
        raise ValueError('exact source page/upload digest bindings required')
    if any(not re.fullmatch(r'[0-9a-f]{64}',i.sha256) or Path(i.filename).name!=i.filename or not i.filename.endswith('.png') for i in images):
        raise ValueError('invalid image binding')
    if MARKER in json.dumps(bundle,ensure_ascii=False): raise ValueError('source contains reserved publication marker')
    key=sha(encode([str(hub_id),bundle,[i.model_dump(mode='json') for i in images]]))
    content=render_study(bundle,images,key)
    canonical(content,images)
    complete_record(dict(metadata={'type':'page'},text='<page>\n<content>\n'+content+'\n</content>\n</page>'))
    if checkpoint is not None:
        current=StudyCheckpoint.model_validate(checkpoint.model_dump())
        if (current.operation_key,current.hub_id,current.expected_content)!=(key,hub_id,content):raise ValueError('checkpoint/source/target mismatch')
        if remote_packet is None:raise ValueError('existing checkpoint requires remote readback; do not repeat create')
        return None,confirm_study(remote_packet,current,images),images
    title=bundle['title']+' · 절 학습 묶음 · '+key[:12]
    cp=StudyCheckpoint(operation_key=key,hub_id=hub_id,title=title,expected_content=content)
    action={'parent':{'page_id':str(hub_id)},'pages':[{'properties':{'title':title},'content':content+'\n\n## 사용자 메모\n학습 의견을 이 영역에 적어주세요.'}]}
    return action,cp,images


def confirm_study(packet, checkpoint, images):
    cp=StudyCheckpoint.model_validate(checkpoint.model_dump());record,body=complete_record(packet)
    page_id=_uuid_from_url(record['url'])
    if record.get('title')!=cp.title:raise ValueError('created page title mismatch')
    if cp.status=='confirmed' and cp.page_id is None:raise ValueError('confirmed checkpoint lacks page binding')
    if cp.page_id and cp.page_id!=page_id: raise ValueError('wrong created page')
    ancestors=re.search(r'<ancestor-path>(.*?)</ancestor-path>',record['text'],re.S)
    parent=re.search(r'<parent-page[^>]*url="([^"]+)"',ancestors[1] if ancestors else '')
    if parent is None or _uuid_from_url(parent[1])!=cp.hub_id:raise ValueError('page outside designated direct hub')
    begin=MARKER+cp.operation_key+':begin';end=MARKER+cp.operation_key+':end'
    decoded=decode_text(body)
    if decoded.count(begin)!=1 or decoded.count(end)!=1:raise ValueError('missing/duplicate ownership markers')
    # The host may escape hyphens in root marker paragraphs. Decode only those
    # paragraph lines; never decode literal code or accept nested marker copies.
    positions={};depth=0;in_code=False;lines=body.splitlines()
    for index,line in enumerate(lines):
        if re.fullmatch(r'\t*```[^`]*',line):
            in_code=not in_code
            continue
        if in_code:continue
        if line.strip()=='<details>':depth+=1
        elif line.strip()=='</details>':depth-=1
        text=decode_text(line)
        if text in (begin,end):
            if depth!=0 or line.startswith('\t'):raise ValueError('markers detached from root paragraphs')
            positions[text]=index
    if set(positions)!={begin,end} or positions[begin]>=positions[end]:raise ValueError('markers detached from root paragraphs')
    owned='\n'.join(lines[positions[begin]:positions[end]+1])
    if canonical(owned,images,native_code_payload=True)!=canonical(cp.expected_content,images):raise ValueError('readback mismatch; preserve pending state')
    return cp.model_copy(update={'page_id':page_id,'status':'confirmed'})


def save_study_checkpoint(path: Path, checkpoint: StudyCheckpoint, *, protected_paths=()):
    checkpoint=StudyCheckpoint.model_validate(checkpoint.model_dump())
    if checkpoint.status=='confirmed' and checkpoint.page_id is None:
        raise ValueError('confirmed checkpoint requires page binding')
    path=Path(path)
    if path.suffix!='.json' or path.name.startswith('.env'):
        raise ValueError('explicit JSON checkpoint required')
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('regular private checkpoint path required')
    if any((p/'.git').exists() for p in [path.parent,*path.parents]):
        raise ValueError('checkpoint must be outside Git')
    for protected in map(Path,protected_paths):
        if path.resolve()==protected.resolve() or (path.exists() and protected.exists() and path.samefile(protected)):
            raise ValueError('checkpoint must not alias inputs or assets')
    before=None
    def regular_target():
        if path.is_symlink():raise ValueError('checkpoint target became a symlink')
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>1_000_000:
            raise ValueError('regular single-link bounded checkpoint required')
    if path.exists():
        regular_target();before=path.read_bytes()
        try:previous=StudyCheckpoint.model_validate_json(before)
        except ValueError as exc:raise ValueError('existing target is not this checkpoint; preserve it') from exc
        if previous.status=='confirmed' and previous.page_id is None:
            raise ValueError('existing confirmed checkpoint lacks page binding')
        identity=lambda cp:(cp.operation_key,cp.hub_id,cp.title,cp.expected_content)
        if identity(previous)!=identity(checkpoint):raise ValueError('different checkpoint identity; preserve pending task')
        if previous.page_id is not None and checkpoint.page_id!=previous.page_id:
            raise ValueError('checkpoint page binding cannot be removed or changed')
        if previous.status=='confirmed' and checkpoint.status!='confirmed':
            raise ValueError('confirmed checkpoint cannot return to pending')
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    if temp.exists() or temp.is_symlink():raise ValueError('existing checkpoint staging path requires review')
    try:
        with temp.open('x',encoding='utf-8') as stream:stream.write(checkpoint.model_dump_json(indent=2))
        if before is None:
            if path.exists() or path.is_symlink():raise ValueError('checkpoint target appeared during save')
        else:
            regular_target()
            if path.read_bytes()!=before:raise ValueError('checkpoint target changed during save')
        temp.replace(path)
    finally:temp.unlink(missing_ok=True)
