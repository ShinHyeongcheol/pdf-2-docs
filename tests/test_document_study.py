import copy
import json
import sqlite3
from pathlib import Path

import pytest
from test_document_lesson import approval, run
from pdf_notion_mvp.document_lesson import BatchDraft,verify_draft,cloze_stem
from pdf_notion_mvp.document_study import accept_batch,item_ids,render_batch,prose
from pdf_notion_mvp.live_run import fingerprint
from pdf_notion_mvp.notion_mcp import decode_text


@pytest.fixture
def saved(approval):
    result,_=run(approval,[])
    path=Path(approval.spec.output_dir)/(result['operation_key'].split(':')[1]+'.json')
    review=dict(result_digest=fingerprint(result),reviewed_ids=item_ids(BatchDraft.model_validate(result['draft'])),
        reviewer='independent fixture reviewer',decision='accepted',notes=['Authored fixture only.'],
        review_kind='independent_source_comparison')
    rp=path.parent/'review.json';rp.write_text(json.dumps(review))
    return path,rp,Path(approval.spec.budget_ledger),result,review


def test_accept_and_render_preserve_source_and_original_receipt(saved):
    path,review,ledger,original,_=saved;before={p:p.read_bytes() for p in [path,review,ledger]}
    material=accept_batch(path,review,ledger)
    content=render_batch(material,'https://www.notion.so/00000000000000000000000000000001')
    assert '정답과 해설' in content and '전체 전사' in content and '[빈칸]' in content
    assert not material['full_ocr_semantics_verified'] and not material['code_execution_verified']
    assert all(p.read_bytes()==data for p,data in before.items())
    assert json.loads(path.read_text())==original


@pytest.mark.parametrize('damage',['missing_item','rejected','digest','duplicate','receipt','source'])
def test_changed_or_partial_review_and_receipts_block_publication(saved,damage):
    path,rp,ledger,result,review=saved
    if damage=='missing_item':review['reviewed_ids'].pop()
    elif damage=='rejected':review['decision']='needs_changes'
    elif damage=='digest':review['result_digest']='0'*64
    elif damage=='duplicate':review['reviewed_ids'].append(review['reviewed_ids'][0])
    elif damage=='receipt':
        with sqlite3.connect(ledger) as db:db.execute('UPDATE reservations SET request_count=2')
    else:Path(result['input_paths'][0]).write_text('{}')
    rp.write_text(json.dumps(review))
    with pytest.raises(ValueError):accept_batch(path,rp,ledger)


def test_failed_provider_receipt_remains_failed_when_reviewed_overlay_is_accepted(saved):
    path,rp,ledger,result,review=saved
    result['status']='failed_content_validation';result['errors']=['source_grounding_validation_failed']
    result['draft']['pages'][0]['exercises'][0]['answer']='unsupported'
    path.write_text(json.dumps(result))
    with sqlite3.connect(ledger) as db:db.execute('UPDATE reservations SET status=?,result_sha=?',('failed',fingerprint(result)))
    corrected=copy.deepcopy(result['draft']);corrected['pages'][0]['exercises'][0]['answer']='문서'
    revision=dict(original_result_path=str(path),original_result_digest=fingerprint(result),reviewed_draft=corrected,
        reviewed_draft_digest=fingerprint(corrected),additional_model_calls=0,edits=[{'field':'answer','before':'unsupported','after':'문서'}])
    rev=path.parent/'revision.json';rev.write_text(json.dumps(revision))
    review['result_digest']=fingerprint(revision);rp.write_text(json.dumps(review))
    before=ledger.read_bytes();original_bytes=path.read_bytes()
    with pytest.raises(ValueError,match='requires'):accept_batch(path,rp,ledger)
    material=accept_batch(path,rp,ledger,revision_path=rev)
    assert material['status']=='source_reviewed_with_local_corrections'
    assert material['provider_result_status']=='failed_content_validation'
    assert ledger.read_bytes()==before and path.read_bytes()==original_bytes


def test_corrected_material_tampering_invalidates_acceptance(saved):
    path,rp,ledger,result,review=saved
    revision=dict(original_result_path=str(path),original_result_digest=fingerprint(result),reviewed_draft=result['draft'],
        reviewed_draft_digest=fingerprint(result['draft']),additional_model_calls=0,edits=[])
    rev=path.parent/'revision.json';review['result_digest']=fingerprint(revision);rp.write_text(json.dumps(review))
    revision['reviewed_draft']['pages'][0]['notes'][0]['text']='changed after review'
    rev.write_text(json.dumps(revision))
    with pytest.raises(ValueError,match='lineage'):accept_batch(path,rp,ledger,revision_path=rev)


def test_visual_candidate_cannot_attach_an_unrelated_image(saved):
    path,rp,ledger,result,review=saved
    revision=dict(original_result_path=str(path),original_result_digest=fingerprint(result),reviewed_draft=result['draft'],
        reviewed_draft_digest=fingerprint(result['draft']),additional_model_calls=0,edits=[],local_visual_notes=[dict(
            note_id='p1.visual1',page=1,text='invented image',source_image_block_id='missing',source={},
            source_image_sha256='0'*64,status='candidate_caption',basis='independent_local_visual_observation',model_images_transmitted=False)])
    review['result_digest']=fingerprint(revision);review['reviewed_ids'].append('p1.visual1')
    rp.write_text(json.dumps(review));rev=path.parent/'revision.json';rev.write_text(json.dumps(revision))
    with pytest.raises(ValueError,match='source-bound'):accept_batch(path,rp,ledger,revision_path=rev)


def test_dotted_code_names_remain_literal_without_native_domain_links():
    assert decode_text(prose('model.stream()과 chunk.content를 예제로 제시합니다.'))=='`model.stream`()과 `chunk.content`를 예제로 제시합니다.'
    assert 'http://' not in prose('model.stream()')


@pytest.mark.parametrize('damage',['note','answer','decision','context','ids','disk_review'])
def test_render_reaccepts_current_files_and_rejects_post_acceptance_mutation(saved,damage):
    path,rp,ledger,_,_=saved;material=accept_batch(path,rp,ledger)
    if damage=='note':material['draft']['pages'][0]['notes'][0]['text']='unsupported new claim'
    elif damage=='answer':material['draft']['pages'][0]['exercises'][0]['answer']='unsupported answer'
    elif damage=='decision':material['review']['decision']='needs_changes'
    elif damage=='context':material['context']['units'][0]['text']='changed source'
    elif damage=='ids':material['review']['reviewed_ids']=[]
    else:
        r=json.loads(rp.read_text());r['decision']='needs_changes';rp.write_text(json.dumps(r))
    with pytest.raises(ValueError):render_batch(material,'https://www.notion.so/00000000000000000000000000000001')


def table_question():
    context=dict(pages=[1],diagrams=[],units=[dict(unit_id=i,text=t,source=dict(page=p)) for i,t,p in
        [('heading','지원 형식 · 로더',1),('answer','NotionDirectoryLoader',1),('format','Notion 페이지',1),('other','다른 페이지',2)]])
    draft=BatchDraft.model_validate(dict(pages=[dict(page=1,title='작성한 표',notes=[],uncertainty='합성 관계 검수 후보',
        exercises=[dict(unit_id='answer',answer='NotionDirectoryLoader',explanation='작성한 표의 로더와 지원 형식 관계 후보',context_unit_ids=['heading','format'])])],diagrams=[]))
    return context,draft


def test_table_relationship_has_exact_source_units_and_keeps_row_clues():
    context,draft=table_question();assert verify_draft(context,draft)
    assert cloze_stem(context,draft.pages[0].exercises[0])=='지원 형식 · 로더\n[빈칸]\nNotion 페이지'
    draft.pages[0].exercises[0].context_unit_ids=[]
    with pytest.raises(ValueError,match='unsupported'):verify_draft(context,draft)


@pytest.mark.parametrize('ids',[['missing'],['other'],['heading','heading'],['answer']])
def test_cloze_context_cannot_cross_pages_or_hide_unknown_duplicate_ids(ids):
    context,draft=table_question();draft.pages[0].exercises[0].context_unit_ids=ids
    with pytest.raises(ValueError,match='context'):verify_draft(context,draft)


@pytest.mark.parametrize('source_url',[
    'https://www.notion.so/"/><mention-page url="https://example.invalid/00000000000000000000000000000001',
    'https://www.notion.so/00000000000000000000000000000001"/><mention-page>',
    'https://www.notion.so/path/00000000000000000000000000000001',
])
def test_source_reference_cannot_inject_native_markup(saved,source_url):
    p,r,b,_,_=saved;material=accept_batch(p,r,b)
    with pytest.raises(ValueError):render_batch(material,source_url)
