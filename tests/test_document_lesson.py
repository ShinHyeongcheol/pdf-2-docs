import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.document_lesson import *
from pdf_notion_mvp.lesson_generation import _budget_connection

ROOT=Path(__file__).parents[1]
NOW=datetime(2026,10,2,tzinfo=timezone.utc)

@pytest.fixture
def approval(tmp_path):
    paths=[];(tmp_path/'inputs').mkdir()
    for name in ['synthetic.json','synthetic-outline.json','synthetic-review.json']:
        p=tmp_path/'inputs'/name;p.write_bytes((ROOT/'fixtures'/name).read_bytes());paths.append(p)
    ledger=tmp_path/'ledger'/'budget.sqlite';_budget_connection(ledger).close()
    a=prepare(paths,[1],output_dir=tmp_path/'results',budget_path=ledger,key_project_root=ROOT,now=NOW)
    for key in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(a,key,True)
    return a


def draft(context):
    u=next(u for u in context['units'] if '문서' in u['text'])
    return {'pages':[{'page':1,'title':'합성 단계','notes':[{'text':'성공한 단계를 보존합니다.','unit_ids':[u['unit_id']]}],
        'exercises':[{'unit_id':u['unit_id'],'answer':'문서','explanation':'원문 용어를 찾습니다.'}],
        'uncertainty':'작성한 합성 입력의 검수 예입니다.'}],'diagrams':[]}


def factory(a,seen,damage=None):
    _,context,_=validate_approval(a,a.plan_sha256,NOW)
    def handler(request):
        seen.append(request)
        if damage=='timeout':raise httpx.ReadTimeout('fabricated-private-key')
        if damage=='http':return httpx.Response(400,json={'error':'fabricated-private-key'})
        data=draft(context)
        if damage=='source':data['pages'][0]['notes'][0]['unit_ids']=['unknown']
        if damage=='coverage':data['pages'][0]['page']=2
        if damage=='answer':data['pages'][0]['exercises'][0]['answer']='없는 정답'
        return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':json.dumps(data)}]},'finishReason':'MAX_TOKENS' if damage=='finish' else 'STOP'}],
            'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':20,'totalTokenCount':120}})
    def build(**kw):
        if damage=='request':
            def attack(r):r._content=b'{}'
            kw['event_hooks']['request'].insert(0,attack)
        if damage=='endpoint':
            def attack(r):r.url=httpx.URL('https://example.invalid/private')
            kw['event_hooks']['request'].insert(0,attack)
        return httpx.Client(transport=httpx.MockTransport(handler),**kw)
    return build


def run(a,seen,damage=None):
    return execute(a,a.plan_sha256,client_factory=factory(a,seen,damage),key_provider=lambda:'fabricated-private-key',now=NOW)


def test_exact_serialization_cached_result_without_key_and_cost_reservation(approval):
    seen=[];result,status=run(approval,seen)
    assert status=='written' and result['status']=='ready_for_content_review' and result['request_count']==1
    _,context,request=validate_approval(approval,approval.plan_sha256,NOW)
    assert json.loads(seen[0].content)==request and result['context']==context
    assert not result['images_transmitted'] and not result['semantic_correctness_verified']
    assert str(ROOT) not in seen[0].content.decode() and 'asset_ref' not in seen[0].content.decode()
    cached,status=execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('second key'),now=NOW)
    assert status=='unchanged' and cached==result
    with sqlite3.connect(approval.spec.budget_ledger) as db:assert db.execute('SELECT sum(cost),sum(request_count) FROM reservations').fetchone()==(COST_MICRO_USD,1)


@pytest.mark.parametrize('gate',['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed','digest','source','expiry'])
def test_gate_before_key_or_reservation(approval,gate):
    if gate in type(approval).model_fields:setattr(approval,gate,False)
    elif gate=='digest':approval.plan_sha256='0'*64
    elif gate=='source':Path(approval.spec.input_paths[0]).write_text('{}')
    else:approval.spec.expires_at=NOW;approval.plan_sha256=fingerprint(approval.spec.model_dump(mode='json'))
    with pytest.raises(ValueError):execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('key'),now=NOW)
    with sqlite3.connect(approval.spec.budget_ledger) as db:assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==0


@pytest.mark.parametrize('damage',['timeout','http','finish','request','endpoint'])
def test_failed_call_consumes_operation_without_retry_and_redacts_error(approval,damage):
    seen=[]
    with pytest.raises((ValueError,httpx.ReadTimeout)):run(approval,seen,damage)
    with pytest.raises(ValueError,match='consumed'):execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('retry'),now=NOW)
    with sqlite3.connect(approval.spec.budget_ledger) as db:
        row=db.execute('SELECT cost,status,request_count,error_code FROM reservations').fetchone()
    assert row[0]==COST_MICRO_USD and row[1]=='failed' and row[2]==(0 if damage in ['request','endpoint'] else 1)
    assert 'fabricated-private-key' not in Path(approval.spec.budget_ledger).read_bytes().decode(errors='ignore')


@pytest.mark.parametrize('damage',['source','coverage','answer'])
def test_source_validation_failed_receipt_preserved_no_retry(approval,damage):
    seen=[];result,_=run(approval,seen,damage)
    assert result['status']=='failed_content_validation' and result['errors']==['source_grounding_validation_failed']
    with pytest.raises(ValueError,match='consumed'):execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('retry'),now=NOW)
    assert len(seen)==1


def test_prior_budget_exhaustion_and_missing_ledger_before_key(approval):
    with sqlite3.connect(approval.spec.budget_ledger) as db:
        db.execute('INSERT INTO reservations(operation,run_id,cost,status) VALUES(?,?,?,?)',('earlier','earlier',BUDGET_MICRO_USD,'failed'))
    with pytest.raises(ValueError,match='exhausted'):execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('key'),now=NOW)
    Path(approval.spec.budget_ledger).unlink()
    with pytest.raises(ValueError,match='existing shared'):execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('key'),now=NOW)
    assert not Path(approval.spec.budget_ledger).exists()


def test_parallel_operation_reserves_once(approval):
    seen=[]
    def go(_):
        try:return run(approval,seen)[1]
        except ValueError:return 'consumed'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(go,[1,2]))
    assert results.count('written')==1 and len(seen)==1


def test_saved_receipt_tampering_blocks_reuse(approval):
    seen=[];run(approval,seen)
    path=next(Path(approval.spec.output_dir).glob('*.json'));data=json.loads(path.read_text());data['draft']['pages'][0]['notes'][0]['text']='changed';path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='changed'):execute(approval,approval.plan_sha256,key_provider=lambda:pytest.fail('key'),now=NOW)


def test_provider_schema_keeps_title_field_and_context_candidate_not_applied():
    from pdf_notion_mvp.rag_cli import load_fixture
    from pdf_notion_mvp.rag_files import ReviewFiles
    f=load_fixture(ROOT);files=ReviewFiles(f.source,f.hierarchy,f.review,[],None)
    context,_=context_for(files,[1]);units={u['unit_id']:u for u in context['units']}
    candidate=next(c for c in f.review.corrections if c.status=='candidate')
    assert units[candidate.block_id]['text']==candidate.original_text
    assert units[candidate.block_id]['proposed_text']==candidate.proposed_text
    assert units[candidate.block_id]['review_status']=='candidate'
    assert 'title' in wire_schema()['$defs']['PageLesson']['properties']

from test_diagram_local import data as diagram_data


def test_image_inline_request_exact_source_and_observation_grounding(diagram_data):
    f,e,path=diagram_data;context,prepared=context_for(f,[1],[e]);request=expected_request(context,prepared)
    parts=request['contents'][0]['parts'];assert len(parts)==3
    assert base64.b64decode(parts[2]['inlineData']['data'])==path.read_bytes()
    assert str(path) not in json.dumps(request) and 'asset_ref' not in json.dumps(request)
    value=BatchDraft.model_validate({'pages':[{'page':1,'title':'작성한 이미지','notes':[], 'exercises':[],'uncertainty':'합성 도표 관찰 후보'}],
        'diagrams':[{'image_block_id':'image','text':'A 라벨 관찰 후보','observation_ids':['label'],'uncertainty':'시각 의미 확정 없음'}]})
    assert verify_draft(context,value)
    value.diagrams[0].observation_ids=['unknown']
    with pytest.raises(ValueError,match='unsupported diagram'):verify_draft(context,value)
    path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='changed'):expected_request(context,prepared)


def test_multi_page_wrong_page_citation_and_unordered_scope(approval):
    files=load_review_files(*[Path(p) for p in approval.spec.input_paths],[],Path(approval.spec.output_dir)/'unused.json')
    context,_=context_for(files,[1,2]);value=draft(context)
    value['pages'].append({'page':2,'title':'재개','notes':[{'text':'근거 페이지 오류','unit_ids':[context['units'][0]['unit_id']]}], 'exercises':[],'uncertainty':'합성 입력'})
    with pytest.raises(ValueError,match='wrong-page'):verify_draft(context,BatchDraft.model_validate(value))
    with pytest.raises(ValueError):context_for(files,[2,1])
    with pytest.raises(ValueError):context_for(files,[True])
