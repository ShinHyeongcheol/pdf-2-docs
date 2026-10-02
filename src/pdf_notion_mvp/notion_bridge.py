"""Run append-only Notion MCP publication through a supplied connected host.

No token, HTTP client or provider call. Hold one private journal lock across
read/dispatch/readback. Unknown writes are read back, never automatically resent.
"""
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from .contracts import Contract
from .lesson_generation import _regular
from .notion_mcp import _uuid_from_url, decode_text, parse_owned, render_block
from .notion_quiz import paragraph, toggle
from .offline_replay import file_digest
from .rag_files import read_json_input
from .study_bundle import encode, private_destination, sha
from .study_notion import complete_record

MARKER = 'pdf-notion-publication:v1:'


class ExistingPage(Contract):
    page_id: UUID
    title: str = Field(min_length=1,max_length=500)
    baseline_path: Path
    baseline_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class BridgeSpec(Contract):
    hub_id: UUID
    hub_title: str = Field(min_length=1,max_length=500)
    pages: list[ExistingPage] = Field(min_length=1,max_length=50)
    title: str = Field(min_length=1,max_length=500)
    paragraphs: list[str] = Field(min_length=1,max_length=8)
    input_sha256: dict[Path,str]
    review_path: Path


def plan_sha256(spec):
    spec=BridgeSpec.model_validate(spec.model_dump())
    return sha(encode(spec.model_dump(mode='json')))


class McpGateway(Protocol):
    mode: Literal['host_connected_mcp']
    def call(self,tool: str,arguments: dict) -> dict: ...


def page_body(packet,identifier,title,hub_id=None):
    record,body=complete_record(packet)
    if _uuid_from_url(record['url'])!=identifier or record.get('title')!=title:
        raise ValueError('wrong Notion page or title')
    if hub_id is not None:
        ancestors=re.search(r'<ancestor-path>(.*?)</ancestor-path>',record['text'],re.S)
        parent=re.search(r'<parent-page\b[^>]*url="([^"]+)"',ancestors[1] if ancestors else '')
        if parent is None or _uuid_from_url(parent[1])!=hub_id:
            raise ValueError('page outside designated direct hub')
    return body


def canonical_native(body,*,image_bindings=None):
    """Compare native read snapshots: literal code, exact stable image origin/path.

    Skip blank presentation lines, decode connector escapes only outside code.
    Image bytes were independently checked previously; this verifies references.
    """
    rows=[];fence=None;images=[]
    for line in body.splitlines():
        opening=re.fullmatch(r'(\t*)```[^`]*',line)
        if opening:
            if fence is None:fence=opening[1]
            elif line==fence+'```':fence=None
            else:raise ValueError('ambiguous native code fence')
            rows.append(['fence',line]);continue
        if fence is not None:
            rows.append(['literal',line]);continue
        image=re.fullmatch(r'!\[([^\]]*)\]\((https://[^)]+)\)',line)
        if image:
            parts=urlsplit(image[2]);origin=(parts.hostname or '',parts.path)
            if parts.scheme!='https' or not origin[0].endswith(('.amazonaws.com','.notion.so','.notion-static.com')):
                raise ValueError('unexpected native image origin')
            images.append(origin);rows.append(['image',image[1],*origin]);continue
        if line.strip():
            def mention(match):
                url=match[1];parts=urlsplit(url)
                if parts.scheme!='https' or parts.hostname not in {'app.notion.com','www.notion.so','notion.so'}:
                    return match[0]
                return '<mention-page url="'+str(_uuid_from_url(url))+'"/>'
            # Native mention names resolve dynamically when a target is renamed.
            # Compare their IDs; preserve escaped text and inline/literal code.
            segments=re.split(r'(`[^`]*`)',line)
            line=''.join(segment if index%2 else re.sub(r'(?<!\\)<mention-page url="([^"]+)"(?:\s*/>|>.*?</mention-page>)',mention,segment)
                         for index,segment in enumerate(segments))
            rows.append(['markdown',decode_text(line)])
    if fence is not None:raise ValueError('unclosed native code')
    if image_bindings is not None and images!=image_bindings:
        raise ValueError('native image reference changed')
    return rows,images


def separate_memo(body):
    # A memo header inside literal source code is not a page memo boundary.
    fence=None;boundaries=[];lines=body.splitlines()
    for index,line in enumerate(lines):
        match=re.fullmatch(r'(\t*)```[^`]*',line)
        if match:
            if fence is None:fence=match[1]
            elif line==fence+'```':fence=None
            continue
        if fence is None and line=='## 사용자 메모':boundaries.append(index)
    if fence is not None or len(boundaries)!=1:raise ValueError('one complete user memo boundary required')
    boundary=boundaries[0]
    return '\n'.join(lines[:boundary]),'\n'.join(lines[boundary:])


def save_state(path,state):
    before=None
    if path.exists():_regular(path);before=path.read_bytes()
    staging=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,prefix='.bridge-',delete=False) as stream:
            staging=Path(stream.name);os.chmod(staging,0o600)
            stream.write(json.dumps(state,ensure_ascii=False));stream.flush();os.fsync(stream.fileno())
        if before is None:
            if path.exists() or path.is_symlink():raise ValueError('journal appeared during save')
        elif file_digest(path)!=sha(before):raise ValueError('journal changed during save')
        staging.replace(path)
    finally:
        if staging is not None:staging.unlink(missing_ok=True)


def run_connected(spec,*,output_dir,gateway):
    spec=BridgeSpec.model_validate(spec.model_dump())
    if gateway.mode!='host_connected_mcp':raise ValueError('connected host MCP required')
    if len({p.page_id for p in spec.pages})!=len(spec.pages) or spec.hub_id in {p.page_id for p in spec.pages}:
        raise ValueError('unique direct child targets required')
    if any(not re.fullmatch(r'[0-9a-f]{64}',value) for value in spec.input_sha256.values()):
        raise ValueError('explicit input digests required')
    paths=list(spec.input_sha256)+[p.baseline_path for p in spec.pages]+[spec.review_path]
    root=private_destination(Path(output_dir),paths);root.mkdir(parents=True,exist_ok=True)
    plan=plan_sha256(spec)
    review=read_json_input(spec.review_path)
    if review.get('decision')!='accepted' or review.get('plan_sha256')!=plan:
        raise ValueError('accepted exact-plan review required')
    frozen={str(path.resolve()):file_digest(path) for path in paths}
    def current():
        private_destination(root,paths)
        if any(file_digest(p)!=value for p,value in spec.input_sha256.items()) or any(file_digest(p.baseline_path)!=p.baseline_sha256 for p in spec.pages):
            raise ValueError('reviewed source or native baseline changed')
        if {str(p.resolve()):file_digest(p) for p in paths}!=frozen:
            raise ValueError('inputs changed during connected publication')
    current()
    key=sha(encode(['connected-notion-publication-v1',str(spec.hub_id),plan]))
    if MARKER in spec.title or any(MARKER in p or len(p)>20_000 for p in spec.paragraphs):
        raise ValueError('bounded literal publication text required')
    block=toggle(spec.title,[paragraph(p) for p in spec.paragraphs]+[toggle('원문 범위와 갱신 기록',[paragraph(MARKER+key)])])
    content=render_block(block)
    if parse_owned(content,marker=MARKER)!={key:block}:raise ValueError('literal renderer roundtrip failed')
    lock=root/'publication.lock';fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        _regular(lock);fcntl.flock(fd,fcntl.LOCK_EX)
        if (os.fstat(fd).st_dev,os.fstat(fd).st_ino)!=(lock.stat().st_dev,lock.stat().st_ino):raise ValueError('publication lock changed')
        current();path=root/'publication.json'
        state=read_json_input(path) if path.exists() else dict(schema_version='1',plan_sha256=plan,key=key,status='new',before_body=None,expected_content=content,verified_pages=[])
        if (state.get('schema_version'),state.get('plan_sha256'),state.get('key'),state.get('expected_content'))!=('1',plan,key,content) or state.get('status') not in {'new','pending_readback','confirmed'}:
            raise ValueError('unknown or changed publication journal')
        def call(tool,args):
            current();response=gateway.call(tool,args);current()
            if response.get('isError'):raise ValueError('Notion connector reported failure')
            return response
        verified=[]
        for item in spec.pages:
            baseline=page_body(read_json_input(item.baseline_path,max_bytes=2_000_000),item.page_id,item.title,spec.hub_id)
            expected,_=separate_memo(baseline);expected_rows,images=canonical_native(expected)
            remote=page_body(call('notion_fetch',dict(id=str(item.page_id))),item.page_id,item.title,spec.hub_id)
            native,memo=separate_memo(remote)
            if canonical_native(native,image_bindings=images)[0]!=expected_rows:
                raise ValueError('existing native lesson changed; preserve content')
            verified.append(dict(page_id=str(item.page_id),content_sha256=sha(encode(expected_rows)),memo_sha256=sha(memo.encode()),image_references=len(images)))
        # Always fetch now, including after confirmed completion. No cached ready flag.
        body=page_body(call('notion_fetch',dict(id=str(spec.hub_id))),spec.hub_id,spec.hub_title)
        existing=parse_owned(body,marker=MARKER)
        if key in existing:
            if existing[key]!=block:raise ValueError('published content edited; preserve it')
            if state['status']=='pending_readback':
                before=state.get('before_body')
                if not isinstance(before,str) or not body.startswith(before):raise ValueError('preexisting hub content changed; preserve it')
            state.update(status='confirmed',verified_pages=verified)
            # A lost local journal adopts only an exact owned block. It cannot
            # attest to preexisting content preservation, and performs no write.
            save_state(path,state);current()
            return dict(status='unchanged',verified_existing_pages=len(verified),actual_notion_appends=0,model_calls=0,key_reads=0,embedding_calls=0,plan_sha256=plan,operation_key=key)
        if state['status']!='new':raise ValueError('unconfirmed prior write; do not blindly retry')
        state.update(status='pending_readback',before_body=body,verified_pages=verified)
        save_state(path,state)
        # The only mutation is an append to the designated hub. No lesson rewrite.
        call('notion_update_page',dict(page_id=str(spec.hub_id),command='insert_content',position={'type':'end'},content=content,allow_async=False))
        after=page_body(call('notion_fetch',dict(id=str(spec.hub_id))),spec.hub_id,spec.hub_title)
        if not after.startswith(body) or parse_owned(after,marker=MARKER).get(key)!=block:
            raise ValueError('append readback failed; retain pending state')
        state.update(status='confirmed');save_state(path,state);current()
        return dict(status='published_readback_verified',verified_existing_pages=len(verified),actual_notion_appends=1,model_calls=0,key_reads=0,embedding_calls=0,plan_sha256=plan,operation_key=key)
    finally:os.close(fd)
