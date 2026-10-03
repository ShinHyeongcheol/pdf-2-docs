import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.lesson_generation import create_proposal,execute,operation_key
from pdf_notion_mvp.transient_retry import observation
from test_free_execution import free_plan
from test_lesson_generation import inputs,NOW,ROOT,factory


def failed(paths,tmp_path):
    a,paid=free_plan(paths,tmp_path);seen=[]
    error={'http_status':503,'packet':{'error':{'code':503,'status':'UNAVAILABLE','message':'authored transient service error'}}}
    with pytest.raises(Exception):run(a,tmp_path,seen,error,NOW)
    assert len(seen)==1
    receipt=Path(a.spec.output_dir)/(operation_key(a.spec)+'-error.json')
    return a,paid,receipt


def proposal(a,path,index,out,now):
    q=create_proposal(a.spec.input_paths,a.spec.section_id,a.spec.exclude_pages,
        output_dir=out,budget_ledger=Path(a.spec.budget_ledger),key_project_root=ROOT,
        now=now,lesson_format='summary_v1',free_tier_evidence=a.spec.free_tier_evidence,
        free_ledger=a.spec.free_ledger,http503_retry_of=path,http503_retry_index=index)
    for k in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(q,k,True)
    return q


def run(a,tmp_path,seen,violation=None,now=NOW):
    from pdf_notion_mvp.rag_files import load_review_files
    from pdf_notion_mvp.lesson_generation import units_for
    files=load_review_files(*map(Path,a.spec.input_paths),[],tmp_path/'unused')
    context=units_for(files,a.spec.section_id,[])
    return execute(a,a.plan_sha256,Path(a.spec.budget_ledger),Path(a.spec.output_dir),ROOT,
        client_factory=factory(context,seen,violation),key_provider=lambda:'fabricated-private-key',now=now,
        free_project_verifier=lambda key,evidence:evidence.project_id,response_clock=lambda:now)


def test_explicit_same_free_request_has_two_consumed_children_and_immutable_root(inputs,tmp_path):
    paths,_=inputs;a,paid,receipt=failed(paths,tmp_path);original=receipt.read_bytes();paid_bytes=paid.read_bytes()
    now=NOW+timedelta(seconds=121)
    one=proposal(a,receipt,1,tmp_path/'retry1',now)
    assert one.spec.request_digest==a.spec.request_digest and one.spec.context_digest==a.spec.context_digest
    assert operation_key(one.spec)!=operation_key(a.spec)
    with pytest.raises(ValueError,match='first observed'):
        proposal(a,receipt,2,tmp_path/'premature2',now)
    seen=[];error={'http_status':503,'packet':{'error':{'code':503,'status':'UNAVAILABLE'}}}
    with pytest.raises(Exception):run(one,tmp_path,seen,error,now)
    with pytest.raises(ValueError,match='wait'):
        proposal(a,receipt,2,tmp_path/'early2',now+timedelta(seconds=119))
    two=proposal(a,receipt,2,tmp_path/'retry2',now+timedelta(seconds=121))
    with pytest.raises(Exception):run(two,tmp_path,seen,error,now+timedelta(seconds=121))
    assert len(seen)==2 and receipt.read_bytes()==original and paid.read_bytes()==paid_bytes
    with pytest.raises(ValueError,match='no automatic retry'):
        run(two,tmp_path,[],now=now+timedelta(seconds=121))
    with pytest.raises(ValueError):proposal(a,receipt,3,tmp_path/'third',now+timedelta(seconds=400))
    db=sqlite3.connect(a.spec.free_ledger)
    assert db.execute('select count(*),sum(cost),sum(request_count) from reservations').fetchone()==(3,0,3)
    db.close()


@pytest.mark.parametrize('error',[429,403,200])
def test_next_retry_blocks_quota_permissions_and_successful_first_child(inputs,tmp_path,error):
    paths,_=inputs;a,paid,receipt=failed(paths,tmp_path);now=NOW+timedelta(seconds=121)
    one=proposal(a,receipt,1,tmp_path/'retry1',now);seen=[]
    if error==200:run(one,tmp_path,seen,now=now)
    else:
        with pytest.raises(Exception):run(one,tmp_path,seen,{'http_status':error,'packet':{'error':{'code':error,'status':'RESOURCE_EXHAUSTED' if error==429 else 'PERMISSION_DENIED'}}},now)
    with pytest.raises(ValueError,match='HTTP503'):
        proposal(a,receipt,2,tmp_path/'forbidden2',now+timedelta(seconds=121))


def test_retry_after_never_logs_raw_header_and_respects_date_or_seconds():
    for header,seconds in [('360',360),('Sat, 03 Oct 2026 10:10:00 GMT',None)]:
        packet=observation(httpx.Response(503,headers={'Retry-After':header}),NOW)
        assert packet['retry_after_present'] and not packet['retry_after_invalid']
        assert header not in json.dumps(packet)
        if seconds:assert packet['retry_not_before']==(NOW+timedelta(seconds=seconds)).isoformat()
    invalid=observation(httpx.Response(503,headers={'Retry-After':'fabricated-private-key'}),NOW)
    assert invalid['retry_after_invalid'] and 'fabricated-private-key' not in json.dumps(invalid)


def test_early_changed_request_and_tampered_root_stop_before_credentials(inputs,tmp_path):
    paths,_=inputs;a,paid,receipt=failed(paths,tmp_path)
    with pytest.raises(ValueError,match='wait'):proposal(a,receipt,1,tmp_path/'early',NOW+timedelta(seconds=119))
    data=json.loads(receipt.read_text());data['diagnostic']['http_status']=429
    receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='HTTP503'):proposal(a,receipt,1,tmp_path/'tampered',NOW+timedelta(seconds=121))


@pytest.mark.parametrize('header',['Sat, 03 Oct 2026 10:10:00 GMT junk','Sat, 03 Oct 2026 10:10:00 GMT, 360'])
def test_retry_after_requires_entire_verified_header(header):
    assert observation(httpx.Response(503,headers={'Retry-After':header}),NOW)['retry_after_invalid']

def test_duplicate_retry_after_is_rejected():
    assert observation(httpx.Response(503,headers=[('Retry-After','120'),('Retry-After','360')]),NOW)['retry_after_invalid']

def test_copied_ledger_cannot_reset_retry_family(inputs,tmp_path):
    paths,_=inputs;a,paid,receipt=failed(paths,tmp_path)
    copied=tmp_path/'copied.sqlite';copied.write_bytes(Path(a.spec.free_ledger).read_bytes())
    a.spec.free_ledger=str(copied)
    with pytest.raises(ValueError,match='ledger identity'):
        proposal(a,receipt,1,tmp_path/'copy-retry',NOW+timedelta(seconds=121))


def test_response_time_is_not_the_approval_preparation_time(inputs,tmp_path,monkeypatch):
    from datetime import datetime
    import pdf_notion_mvp.lesson_generation as generation
    from test_free_execution import execute_mock
    later=NOW+timedelta(seconds=60)
    class ResponseDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return later
    monkeypatch.setattr(generation,'datetime',ResponseDatetime)
    paths,_=inputs;a,_=free_plan(paths,tmp_path)
    with pytest.raises(Exception):
        execute_mock(a,tmp_path,[],{'http_status':503,'packet':{'error':{'code':503,'status':'UNAVAILABLE'}}})
    packet=json.loads((Path(a.spec.output_dir)/(operation_key(a.spec)+'-error.json')).read_text())
    assert datetime.fromisoformat(packet['observed_at'])==later
    assert datetime.fromisoformat(packet['retry_not_before'])==later+timedelta(seconds=120)


def test_unbound_historical_receipt_is_not_retryable(inputs,tmp_path):
    from pdf_notion_mvp.live_run import fingerprint
    paths,_=inputs;a,_,receipt=failed(paths,tmp_path)
    packet=json.loads(receipt.read_text())
    for field in ('execution_ledger','ledger_device','ledger_inode'):packet.pop(field)
    receipt.write_text(json.dumps(packet))
    with sqlite3.connect(a.spec.free_ledger) as db:
        db.execute('update reservations set result_sha=? where operation=?',(fingerprint(packet),operation_key(a.spec)))
    with pytest.raises(ValueError,match='historical HTTP503'):
        proposal(a,receipt,1,tmp_path/'legacy-child',NOW+timedelta(seconds=121))
