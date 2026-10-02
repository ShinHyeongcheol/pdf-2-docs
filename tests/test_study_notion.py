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


@pytest.mark.parametrize('literal',['XML 교육 예제 <content>원문</content> 끝입니다.','XML 교육 예제 <unknown>원문</unknown> 끝입니다.','<content>\n<unknown>직접 작성한 원문</unknown>\n</content>\n</page>'])
def test_fenced_wrapper_and_unknown_examples_preserve_exact_source(planned,literal):
    files,hub,_,_,images=planned
    from pdf_notion_mvp.review import document_digest
    files.source.document.blocks[1].text=literal;files.review.source_digest=document_digest(files.source.document)
    before=files.source.model_dump_json()
    action,cp,images=prepare_study(files,'rag.control','교육',HUB,hub,images)
    assert '```text\n'+literal+'\n```' in action['pages'][0]['content']
    assert confirm_study(fetched(cp,images),cp,images).status=='confirmed'
    assert files.source.model_dump_json()==before


@pytest.mark.parametrize('body',['<unknown url="authored-unsupported"/>','<content>\nextra\n</content>','```text\n<content>never closed'])
def test_structural_unknown_or_malformed_wrapper_is_still_rejected(planned,body):
    from pdf_notion_mvp.study_notion import complete_record
    with pytest.raises(ValueError):complete_record(packet(HUB,body))


def test_source_and_other_pending_identity_are_preserved(planned,tmp_path):
    files,hub,_,cp,images=planned
    source=tmp_path/'source.json';source.write_text(files.source.model_dump_json());before=source.read_bytes()
    with pytest.raises(ValueError):save_study_checkpoint(source,cp)
    assert source.read_bytes()==before
    state=tmp_path/'checkpoint.json';save_study_checkpoint(state,cp);before=state.read_bytes()
    _,other,_=prepare_study(files,'rag.control','다른 합성 질문',HUB,hub,images)
    with pytest.raises(ValueError):save_study_checkpoint(state,other)
    assert state.read_bytes()==before


@pytest.mark.parametrize('field',['hub_id','title','expected_content','page_id','status'])
def test_checkpoint_identity_page_binding_and_monotonic_state(planned,tmp_path,field):
    cp=planned[3].model_copy(update={'status':'confirmed','page_id':PAGE})
    path=tmp_path/'checkpoint.json';save_study_checkpoint(path,cp);before=path.read_bytes()
    changes=dict(hub_id=PAGE,title='different task',expected_content=cp.expected_content+'edited',page_id=HUB,status='pending_readback')
    with pytest.raises(ValueError):save_study_checkpoint(path,cp.model_copy(update={field:changes[field]}))
    assert path.read_bytes()==before


@pytest.mark.parametrize('kind',['hardlink','protected_input','nonregular','race'])
def test_checkpoint_alias_and_changed_destination_protection(planned,tmp_path,monkeypatch,kind):
    import os
    cp=planned[3];state=tmp_path/'checkpoint.json'
    if kind=='hardlink':
        save_study_checkpoint(state,cp);os.link(state,tmp_path/'other.json')
        before=state.read_bytes()
        with pytest.raises(ValueError):save_study_checkpoint(state,cp)
        assert state.read_bytes()==before
    if kind=='protected_input':
        save_study_checkpoint(state,cp);before=state.read_bytes()
        with pytest.raises(ValueError):save_study_checkpoint(state,cp,protected_paths=[state])
        assert state.read_bytes()==before
    if kind=='nonregular':
        state.mkdir()
        with pytest.raises(ValueError):save_study_checkpoint(state,cp)
        assert state.is_dir()
    if kind=='race':
        # A user file appearing during temporary write must survive the replace boundary.
        old_open=Path.open
        def appeared(path,*a,**kw):
            if path.name.endswith('.tmp'):state.write_text('user file appeared')
            return old_open(path,*a,**kw)
        monkeypatch.setattr(Path,'open',appeared)
        with pytest.raises(ValueError):save_study_checkpoint(state,cp)
        assert state.read_text()=='user file appeared' and not state.with_name(state.name+'.tmp').exists()


@pytest.fixture
def source_with_inline_diagram(tmp_path):
    import hashlib
    from test_study_bundle import PNG,ocr_lesson
    from pdf_notion_mvp.review import document_digest
    fixture=load_fixture(Path(__file__).parents[1]);root=tmp_path/'authored-rasters';ocr_lesson(fixture,root)
    full=next(b for b in fixture.source.document.blocks if b.kind=='image' and b.source.page==1)
    crop=full.model_copy(deep=True);crop.block_id='authored-inline-diagram';crop.source.method='ocr';crop.source.confidence=0.7
    crop.source.bbox.x0,crop.source.bbox.y0,crop.source.bbox.x1,crop.source.bbox.y1=40,100,550,280
    payload=PNG+b'authored distinct image';path=root/'diagram.png';path.write_bytes(payload)
    crop.asset_ref=str(path);crop.sha256=hashlib.sha256(payload).hexdigest()
    fixture.source.document.blocks.insert(5,crop);fixture.hierarchy.nodes[1].block_ids.append(crop.block_id)
    fixture.review.source_digest=document_digest(fixture.source.document)
    return ReviewFiles(fixture.source,fixture.hierarchy,fixture.review,[],root),full,crop


@pytest.mark.parametrize('use_crop',[False,True])
def test_inline_diagram_cannot_substitute_full_page_raster(source_with_inline_diagram,use_crop):
    files,full,crop=source_with_inline_diagram;chosen=crop if use_crop else full
    image=UploadedPageImage(page=1,sha256=chosen.sha256,file_upload_id=UUID('00000000-0000-4000-8000-000000000003'),filename='authored.png')
    if use_crop:
        with pytest.raises(ValueError):prepare_study(files,'rag.control','확정',HUB,packet(HUB,'authored hub'),[image])
    else:
        action,cp,images=prepare_study(files,'rag.control','확정',HUB,packet(HUB,'authored hub'),[image])
        assert images[0].sha256==full.sha256 and crop.sha256 in action['pages'][0]['content']
        assert cp.status=='pending_readback'


def test_corrupt_confirmed_checkpoint_cannot_acquire_an_unproven_page(planned,tmp_path):
    cp=planned[3];path=tmp_path/'checkpoint.json'
    path.write_text(cp.model_copy(update={'status':'confirmed'}).model_dump_json());before=path.read_bytes()
    with pytest.raises(ValueError):save_study_checkpoint(path,cp.model_copy(update={'status':'confirmed','page_id':PAGE}))
    assert path.read_bytes()==before


def test_candidate_proposal_uses_json_to_preserve_initial_indentation(planned):
    files,hub,_,_,images=planned
    candidate=next(c for c in files.review.corrections if c.status=='candidate')
    candidate.proposed_text='    authored_indented_candidate()'
    _,cp,imgs=prepare_study(files,'rag.control','체크포인트',HUB,hub,images)
    tokens=canonical(cp.expected_content,imgs)
    proposals=[t for t in tokens if t[0]=='literal_code' and t[2]=='json' and '\"proposed_text\": \"    authored_indented_candidate()\"' in t[3]]
    assert proposals
    assert confirm_study(fetched(cp,imgs),cp,imgs).status=='confirmed'
