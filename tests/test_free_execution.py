import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from pdf_notion_mvp.free_execution import FreeTierEvidence
from pdf_notion_mvp.lesson_generation import (ContentReview,LessonSpec,_budget_connection,
    create_proposal,execute,fingerprint,operation_key,units_for,write_reading_bundle)
from pdf_notion_mvp.rag_files import load_review_files
from test_lesson_generation import NOW,ROOT,inputs,factory


def free_plan(paths,tmp_path):
    paid=tmp_path/'paid/budget.sqlite';db=_budget_connection(paid)
    db.execute('INSERT INTO reservations(operation,run_id,cost,status) VALUES(?,?,?,?)',
               ('authored-paid-history','authored-paid-history',9768154,'failed'));db.close()
    png=tmp_path/'evidence/free.png';png.parent.mkdir();png.write_bytes(b'\x89PNG\r\n\x1a\nauthored metadata fixture')
    evidence=FreeTierEvidence(project_id='authored-free-project',verified_at=NOW,expires_at=NOW+timedelta(hours=1),
        image_paths=[str(png)],image_sha256=[hashlib.sha256(png.read_bytes()).hexdigest()])
    a=create_proposal(paths,'unit.part',lesson_format='summary_v1',output_dir=tmp_path/'results',budget_ledger=paid,key_project_root=ROOT,
        free_tier_evidence=evidence,free_ledger=tmp_path/'free/runs.sqlite',now=NOW)
    for name in ('user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed'):
        setattr(a,name,True)
    return a,paid


def execute_mock(a,tmp_path,seen,violation=None,verifier=None):
    files=load_review_files(*[Path(p) for p in a.spec.input_paths],[],tmp_path/'unused.json')
    context=units_for(files,a.spec.section_id,[])
    return execute(a,a.plan_sha256,Path(a.spec.budget_ledger),Path(a.spec.output_dir),ROOT,
        client_factory=factory(context,seen,violation),key_provider=lambda:'fabricated-private-key',now=NOW,
        free_project_verifier=verifier or (lambda key,evidence:'authored-free-project'))


def test_verified_free_execution_preserves_paid_bytes_and_renders_from_separate_receipt(inputs,tmp_path):
    paths,paid_proposal=inputs;a,paid=free_plan(paths,tmp_path);before=paid.read_bytes();seen=[]
    assert operation_key(a.spec)!=operation_key(paid_proposal.spec)
    result,status=execute_mock(a,tmp_path,seen)
    assert status=='written' and result['billing_mode']=='verified_free' and result['usage_estimated_usd']=='0'
    assert result['conditional_cost_micro_usd']==0 and result['cumulative_reserved_micro_usd']==9768154
    assert paid.read_bytes()==before and len(seen)==1 and not result['provider_charge_confirmed']
    db=sqlite3.connect(a.spec.free_ledger)
    assert db.execute('SELECT cost,status,request_count FROM reservations').fetchone()==(0,'completed',1);db.close()
    assert execute(a,a.plan_sha256,paid,Path(a.spec.output_dir),ROOT,now=NOW,
        client_factory=lambda **kw:pytest.fail('second client'),key_provider=lambda:pytest.fail('second key'))==(result,'unchanged')
    ids=[c['claim_id'] for t in result['draft']['topics'] for c in t['claims']]
    ids += [q['question_id'] for q in result['draft']['exercises']]+[q['explanation']['claim_id'] for q in result['draft']['exercises']]
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=ids,reviewer='authored independent fixture',decision='accepted',notes=[])
    rp=tmp_path/'reviews/content.json';rp.parent.mkdir();rp.write_text(review.model_dump_json())
    files=load_review_files(*paths,[],tmp_path/'unused.json')
    final,status=write_reading_bundle(Path(a.spec.output_dir)/(result['operation_key']+'.json'),rp,
        files,a.spec.section_id,'체크포인트',tmp_path/'reading',budget_path=paid)
    assert status=='written' and (final/'index.html').is_file() and paid.read_bytes()==before


def test_wrong_current_project_stops_before_http_and_preserves_paid_ledger(inputs,tmp_path):
    paths,_=inputs;a,paid=free_plan(paths,tmp_path);before=paid.read_bytes();seen=[]
    with pytest.raises(ValueError,match='project differs'):
        execute_mock(a,tmp_path,seen,verifier=lambda key,evidence:'different-free-project')
    assert not seen and paid.read_bytes()==before
    db=sqlite3.connect(a.spec.free_ledger);assert db.execute('SELECT count(*) FROM reservations').fetchone()==(0,);db.close()


def test_free_quota_failure_consumed_without_paid_fallback_or_retry(inputs,tmp_path):
    paths,_=inputs;a,paid=free_plan(paths,tmp_path);before=paid.read_bytes();seen=[]
    error={'http_status':429,'packet':{'error':{'code':429,'status':'RESOURCE_EXHAUSTED','message':'authored free quota rejection'}}}
    with pytest.raises(Exception):execute_mock(a,tmp_path,seen,error)
    assert len(seen)==1 and paid.read_bytes()==before
    db=sqlite3.connect(a.spec.free_ledger)
    assert db.execute('SELECT cost,status,request_count FROM reservations').fetchone()==(0,'failed',1);db.close()
    with pytest.raises(ValueError,match='consumed'):
        execute(a,a.plan_sha256,paid,Path(a.spec.output_dir),ROOT,now=NOW,key_provider=lambda:pytest.fail('retry key'))
    assert paid.read_bytes()==before


@pytest.mark.parametrize('changed',['expired','screenshot','no_verifier'])
def test_free_proof_required_before_network(inputs,tmp_path,changed):
    paths,_=inputs;a,paid=free_plan(paths,tmp_path);before=paid.read_bytes()
    if changed=='expired':
        a.spec.free_tier_evidence.expires_at=NOW;a.plan_sha256=fingerprint(a.spec.model_dump(mode='json'))
    elif changed=='screenshot':Path(a.spec.free_tier_evidence.image_paths[0]).write_bytes(b'changed')
    with pytest.raises(ValueError):
        execute(a,a.plan_sha256,paid,Path(a.spec.output_dir),ROOT,now=NOW,
                key_provider=lambda:pytest.fail('key read'),client_factory=lambda **kw:pytest.fail('client'))
    assert paid.read_bytes()==before


def test_legacy_paid_serialization_and_policy_cannot_waive_cost(inputs):
    _,a=inputs;payload=a.spec.model_dump(mode='json')
    assert not {'billing_mode','free_ledger','free_tier_evidence'} & payload.keys()
    assert LessonSpec.model_validate(payload).model_dump(mode='json')==payload
    payload['conditional_cost_micro_usd']=0
    with pytest.raises(ValueError):LessonSpec.model_validate(payload)
