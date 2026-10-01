import json
import re
from pathlib import Path
from uuid import UUID

import pytest

from pdf_notion_mvp.rag_cli import load_fixture
from pdf_notion_mvp.rag_files import ReviewFiles
from pdf_notion_mvp.study_notion import MARKER, canonical, confirm_study, prepare_study, save_study_checkpoint, StudyCheckpoint, UploadedPageImage

HUB=UUID('00000000-0000-4000-8000-000000000001')
PAGE=UUID('00000000-0000-4000-8000-000000000002')


def packet(page,body,title='hub',parent=HUB,**flags):
    return dict(metadata={'type':'page'},url='https://app.notion.com/p/'+page.hex,title=title,
        text=f'<page>\n<ancestor-path>\n<parent-page url="https://app.notion.com/p/{parent.hex}"/>\n</ancestor-path>\n<content>\n{body}\n</content>\n</page>',**flags)


@pytest.fixture
def planned():
    fixture=load_fixture(Path(__file__).parents[1]);files=ReviewFiles(fixture.source,fixture.hierarchy,fixture.review,[],None)
    image=next(b for b in files.source.document.blocks if b.kind=='image')
    images=[UploadedPageImage(page=1,sha256=image.sha256,file_upload_id=UUID('00000000-0000-4000-8000-000000000003'),filename='authored-page.png')]
    hub=packet(HUB,'existing source child page and user notes')
    action,cp,bindings=prepare_study(files,'rag.control','체크포인트',HUB,hub,images)
    return files,hub,action,cp,bindings


def fetched(cp,images):
    source='file-upload://'+str(images[0].file_upload_id)
    body=cp.expected_content.replace(source,'https://private.s3.amazonaws.com/opaque/authored-page.png?fresh=1')
    def native_code(match):
        prefix,language,payload=match.groups()
        literal='\n'.join(line[len(prefix):] for line in payload.split('\n'))
        return prefix+'```'+language+'\n'+literal+'\n'+prefix+'```'
    body=re.sub(r'(?m)^(\t*)```([^`\n]*)\n(.*?)\n\1```',native_code,body,flags=re.S)
    return packet(PAGE,body+'\n\n## 사용자 메모\nuntouched user notes',cp.title)


def test_create_is_new_direct_child_and_never_updates_source_or_memos(planned):
    files,hub,action,cp,images=planned
    assert action['parent']=={'page_id':str(HUB)} and len(action['pages'])==1
    assert 'page_id' not in action['pages'][0] and action['pages'][0]['properties']['title']==cp.title
    assert cp.status=='pending_readback' and cp.page_id is None
    body=action['pages'][0]['content']
    assert '모델 생성 아님' in body and '실제 모델 답변 아님' in body
    assert '교정 후보 · 본문 미적용' in body and '사용자 메모' in body
    assert images[0].sha256 in body and str(images[0].file_upload_id) in body
    assert MARKER+cp.operation_key+':begin' in body and MARKER+cp.operation_key+':end' in body


def test_readback_resume_and_note_changes_never_create_duplicates(planned,tmp_path):
    files,hub,_,cp,images=planned
    path=tmp_path/'private/checkpoint.json';save_study_checkpoint(path,cp)
    restored=StudyCheckpoint.model_validate_json(path.read_text())
    with pytest.raises(ValueError,match='do not repeat create'):
        prepare_study(files,'rag.control','체크포인트',HUB,hub,images,restored)
    remote=fetched(cp,images)
    action,confirmed,_=prepare_study(files,'rag.control','체크포인트',HUB,hub,images,restored,remote)
    assert action is None and confirmed.status=='confirmed' and confirmed.page_id==PAGE
    save_study_checkpoint(path,confirmed)
    remote['text']=remote['text'].replace('untouched user notes','changed user notes')
    assert prepare_study(files,'rag.control','체크포인트',HUB,hub,images,confirmed,remote)[0] is None
    # Fresh signed URLs do not create a second page or invalidate source evidence.
    remote['text']=remote['text'].replace('fresh=1','fresh=2')
    assert confirm_study(remote,confirmed,images)==confirmed


@pytest.mark.parametrize('damage',['hub','page','title','unknown','truncated','wrapper','marker','duplicate','text','image','code','nesting'])
def test_tampered_or_incomplete_readback_retains_pending_state(planned,damage):
    _,_,_,cp,images=planned;remote=fetched(cp,images);before=cp.model_dump_json()
    if damage=='hub':remote['text']=remote['text'].replace(HUB.hex,PAGE.hex)
    if damage=='page':cp=cp.model_copy(update={'page_id':HUB});before=cp.model_dump_json()
    if damage=='title':remote['title']='changed'
    if damage=='unknown':remote['unknown_block_count']=1
    if damage=='truncated':remote['truncated']=True
    if damage=='wrapper':remote['text']=remote['text'].replace('</page>','')
    if damage=='marker':remote['text']=remote['text'].replace(':begin',':gone')
    if damage=='duplicate':remote['text']=remote['text'].replace('<content>','<content>\n'+MARKER+cp.operation_key+':begin')
    if damage=='text':remote['text']=remote['text'].replace('교정 없음','unsupported label')
    if damage=='image':remote['text']=remote['text'].replace('authored-page.png','wrong-page.png')
    if damage=='code':remote['text']=remote['text'].replace('"original_text":','"wrong_snapshot":',1)
    if damage=='nesting':remote['text']=remote['text'].replace('\t```json','```json',1)
    with pytest.raises(ValueError):confirm_study(remote,cp,images)
    assert cp.model_dump_json()==before and cp.status=='pending_readback'


@pytest.mark.parametrize('damage',['digest','duplicate','missing','filename','upload','source','question','target'])
def test_source_image_checkpoint_and_target_mismatch_blocked(planned,damage):
    files,hub,_,cp,images=planned
    if damage=='digest':images[0].sha256='0'*64
    if damage=='duplicate':images.append(images[0])
    if damage=='missing':images=[]
    if damage=='filename':images[0].filename='../private.png'
    if damage=='upload':images[0].file_upload_id=HUB
    if damage=='source':files.review.source_digest='0'*64
    question='changed' if damage=='question' else '체크포인트'
    target=PAGE if damage=='target' else HUB
    with pytest.raises(ValueError):prepare_study(files,'rag.control',question,target,hub,images,cp)


def test_markdown_injection_cannot_become_remote_blocks(planned):
    from pdf_notion_mvp.review import document_digest
    files,hub,_,_,images=planned
    payload='# injected\n<page url="https://evil.example">move</page>\n[link](https://evil.example)'
    files.source.document.blocks[1].text=payload
    files.review.source_digest=document_digest(files.source.document)
    action,_,_=prepare_study(files,'rag.control','체크포인트',HUB,hub,images)
    body=action['pages'][0]['content']
    assert '```text\n'+payload+'\n```' in body


def test_literal_code_backslashes_and_newlines_are_not_decoded(planned):
    _,_,_,_,images=planned
    text='line\\n and \\t\n    indented\n\nlast'
    body='![원본 p1](file-upload://'+str(images[0].file_upload_id)+')\n```python\n'+text+'\n```'
    assert ['literal_code','','python',text] in canonical(body,images)


def test_no_key_provider_or_network_calls_in_prepare(planned,monkeypatch):
    import socket
    from pdf_notion_mvp import providers
    def forbidden(*a,**kw):pytest.fail('actual credentials/model/network not allowed')
    monkeypatch.setattr(providers,'selected_key',forbidden);monkeypatch.setattr(providers,'build_provider',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    files,hub,_,_,images=planned
    assert prepare_study(files,'rag.control','체크포인트',HUB,hub,images)[0]


def test_checkpoint_private_and_staging_alias_protection(planned,tmp_path):
    cp=planned[3]
    (tmp_path/'.git').mkdir()
    with pytest.raises(ValueError):save_study_checkpoint(tmp_path/'state.json',cp)
    safe=tmp_path.parent/('authored-checkpoint-'+tmp_path.name)
    safe.mkdir();source=safe/'keep.json';source.write_text('keep')
    temp=safe/'state.json.tmp';temp.symlink_to(source)
    with pytest.raises(ValueError):save_study_checkpoint(safe/'state.json',cp)
    assert source.read_text()=='keep'


def test_native_root_marker_escaping_reconciles_without_recreating(planned):
    _,_,_,cp,images=planned
    remote=fetched(cp,images)
    remote['text']=remote['text'].replace('pdf-notion-study:v1:','pdf\\-notion\\-study:v1:')
    assert confirm_study(remote,cp,images).status=='confirmed'


@pytest.mark.parametrize('wrap',['details','code'])
def test_marker_copies_inside_code_or_toggle_cannot_confirm(planned,wrap):
    _,_,_,cp,images=planned
    remote=fetched(cp,images)
    begin=MARKER+cp.operation_key+':begin'
    replacement=('<details>\n<summary>detached</summary>\n\t'+begin+'\n</details>') if wrap=='details' else ('```text\n'+begin+'\n```')
    remote['text']=remote['text'].replace(begin,replacement)
    with pytest.raises(ValueError):confirm_study(remote,cp,images)


def test_native_code_literal_payload_retains_tabs_and_backslashes(planned):
    files,hub,_,_,images=planned
    from pdf_notion_mvp.review import DerivedFragment
    payload='\tprint("authored\\n")\n\t\tprint("next")'
    files.review.fragments.append(DerivedFragment(fragment_id='native-code-tabs',section_id='rag.control',source_block_ids=['rag-corrected'],kind='code',text=payload,status='confirmed',basis='authored',correction_ids=['rag-fix']))
    _,cp,images=prepare_study(files,'rag.control','체크포인트',HUB,hub,images)
    assert confirm_study(fetched(cp,images),cp,images).status=='confirmed'


def test_lost_native_code_nesting_is_not_normalized_away(planned):
    _,_,_,cp,images=planned
    remote=fetched(cp,images)
    remote['text']=remote['text'].replace('\t```json','```json',1).replace('\t```\n','```\n',1)
    with pytest.raises(ValueError):confirm_study(remote,cp,images)


def test_source_bullets_numbers_domains_and_quotes_are_literal(planned):
    files,hub,_,_,images=planned
    from pdf_notion_mvp.review import document_digest
    payload='• authored item\n2. numbered record\nfrom langchain.chat_models import authored'
    files.source.document.blocks[1].text=payload;files.review.source_digest=document_digest(files.source.document)
    action,cp,images=prepare_study(files,'rag.control','체크포인트',HUB,hub,images)
    assert '```text\n'+payload+'\n```' in action['pages'][0]['content']
    assert confirm_study(fetched(cp,images),cp,images).status=='confirmed'


@pytest.mark.parametrize('payload',['authored ``` delimiter','line\rnext','line\u2028next'])
def test_unsupported_literal_source_is_blocked_before_remote_action(planned,payload):
    files,hub,_,_,images=planned
    from pdf_notion_mvp.review import document_digest
    files.source.document.blocks[1].text=payload;files.review.source_digest=document_digest(files.source.document)
    with pytest.raises(ValueError):prepare_study(files,'rag.control','체크포인트',HUB,hub,images)
