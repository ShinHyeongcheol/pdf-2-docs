import copy
import json
import sqlite3
from pathlib import Path

import pytest
from test_document_lesson import approval, run
from pdf_notion_mvp.document_lesson import BatchDraft
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
