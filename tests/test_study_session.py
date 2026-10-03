"""End-to-end session with authored fixture and actual SDK over a fake HTTP transport."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from pdf_notion_mvp.lesson_generation import ContentReview,fingerprint,operation_key
from pdf_notion_mvp.study_session import _load,generate,main,plan,prepare,render
from pdf_notion_mvp.rag_files import load_review_files
from pdf_notion_mvp.lesson_generation import units_for
from test_lesson_generation import NOW,ROOT,inputs as lesson_inputs,factory


@pytest.fixture
def inputs(lesson_inputs):
    paths,a=lesson_inputs
    outline=json.loads(paths[1].read_text())
    for node in outline['nodes']:node['review_status']='confirmed'
    paths[1].write_text(json.dumps(outline))
    return paths,a


def session_for(a,tmp_path):
    return prepare(a.spec.input_paths,a.spec.section_id,'체크포인트',tmp_path/'session',
                   tmp_path/'ledger/budget.sqlite',ROOT,now=NOW)


def test_session_review_plan_generation_render_and_repeat_are_connected(inputs,tmp_path):
    _,a=inputs;session,status=session_for(a,tmp_path)
    assert status=='written' and not (tmp_path/'ledger').exists()
    assert not session.proposal.user_approved
    assert (Path(session.offline_bundle)/'index.html').is_file()
    manifest=tmp_path/'session/session.json';before=manifest.read_bytes();mtime=manifest.stat().st_mtime_ns
    assert session_for(a,tmp_path)==(session,'unchanged')
    assert manifest.read_bytes()==before and manifest.stat().st_mtime_ns==mtime
    assert _load(tmp_path/'session')==session
    proposal=plan(session,now=NOW+timedelta(minutes=1))
    assert proposal.plan_sha256!=session.proposal.plan_sha256 and not proposal.user_approved
    approval=proposal.model_copy(deep=True)
    for key in ('user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed'):
        setattr(approval,key,True)
    approval_path=tmp_path/'approved.json';approval_path.write_text(approval.model_dump_json())
    files=load_review_files(*[Path(p) for p in approval.spec.input_paths],[],tmp_path/'unused.json')
    context=units_for(files,approval.spec.section_id,[]);seen=[]
    kwargs=dict(client_factory=factory(context,seen),key_provider=lambda:'fabricated-private-key',now=NOW+timedelta(minutes=2))
    result,status=generate(session,approval_path,approval.plan_sha256,**kwargs)
    assert status=='written' and len(seen)==1 and result['execution_mode']=='injected'
    assert generate(session,approval_path,approval.plan_sha256,
                    client_factory=lambda **kw:pytest.fail('second client'),key_provider=lambda:pytest.fail('second key'),
                    now=NOW+timedelta(minutes=2))==(result,'unchanged')
    ids=[c['claim_id'] for t in result['draft']['topics'] for c in t['claims']]
    ids += [q['question_id'] for q in result['draft']['exercises']]
    ids += [q['explanation']['claim_id'] for q in result['draft']['exercises']]
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=ids,
        reviewer='authored synthetic comparison fixture',decision='needs_changes',notes=[])
    path=tmp_path/'content-review/review.json';path.parent.mkdir();path.write_text(review.model_dump_json())
    with pytest.raises(ValueError,match='independent'):render(session,path)
    assert not (tmp_path/'session/reading').exists()
    review.decision='accepted';path.write_text(review.model_dump_json())
    final,status=render(session,path)
    assert status=='written' and (final/'index.html').is_file()
    assert '<h1>페이지를 잇는 합성 절</h1>' in (final/'index.html').read_text()
    assert '전체 162페이지' not in (final/'index.html').read_text()
    assert render(session,path)==(final,'unchanged') and len(seen)==1


@pytest.mark.parametrize('changed',['source','scope','approval','manifest'])
def test_changed_session_blocks_before_key(inputs,tmp_path,changed):
    paths,a=inputs;session,_=session_for(a,tmp_path)
    approval=session.proposal.model_copy(deep=True)
    for key in ('user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed'):
        setattr(approval,key,True)
    if changed=='source':
        paths[0].write_bytes(paths[0].read_bytes()+b' ')
        with pytest.raises(ValueError,match='inputs changed'):_load(tmp_path/'session')
    elif changed=='scope':
        with pytest.raises(ValueError,match='scope changed'):
            prepare(a.spec.input_paths,a.spec.section_id,'새 질문',tmp_path/'session',tmp_path/'ledger/budget.sqlite',ROOT,now=NOW)
    elif changed=='manifest':
        path=tmp_path/'session/session.json';packet=json.loads(path.read_text());packet['session']['question']='tampered'
        path.write_text(json.dumps(packet))
        with pytest.raises(ValueError,match='session changed'):_load(tmp_path/'session')
    else:
        approval.spec.section_id='different';approval.plan_sha256=fingerprint(approval.spec.model_dump(mode='json'))
        p=tmp_path/'approved.json';p.write_text(approval.model_dump_json())
        with pytest.raises(ValueError,match='scope'):
            generate(session,p,approval.plan_sha256,key_provider=lambda:pytest.fail('key'),now=NOW)
    assert not (tmp_path/'ledger').exists()


def test_cli_unapproved_plan_is_actionable_and_does_not_read_key(inputs,tmp_path,capsys):
    _,a=inputs;session,_=session_for(a,tmp_path)
    p=tmp_path/'proposal.json';p.write_text(session.proposal.model_dump_json())
    with pytest.raises(SystemExit) as exc:
        main(['generate','--session-dir',str(tmp_path/'session'),'--approval',str(p),
              '--expected-plan-sha256',session.proposal.plan_sha256])
    assert exc.value.code==1 and 'approval_required' in capsys.readouterr().err
    assert not (tmp_path/'ledger').exists()


def test_candidate_outline_stops_before_artifacts_or_model_access(lesson_inputs,tmp_path):
    _,a=lesson_inputs
    with pytest.raises(ValueError,match='confirmed global outline'):
        session_for(a,tmp_path)
    assert not (tmp_path/'session').exists() and not (tmp_path/'ledger').exists()


def test_cli_cached_generation_returns_recovered_file_path_without_key_or_transport(inputs,tmp_path,capsys,monkeypatch):
    from pdf_notion_mvp.lesson_generation import recover_positional_ids
    from pdf_notion_mvp import study_session
    paths,a=inputs;now=NOW
    session,_=prepare(paths,a.spec.section_id,'체크포인트',tmp_path/'session',tmp_path/'ledger/budget.sqlite',ROOT,now=now)
    approval=session.proposal.model_copy(deep=True)
    for key in ('user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed'):setattr(approval,key,True)
    ap=tmp_path/'approved.json';ap.write_text(approval.model_dump_json())
    files=load_review_files(*paths,[],tmp_path/'unused.json');context=units_for(files,a.spec.section_id,[]);seen=[]
    original,_=generate(session,ap,approval.plan_sha256,client_factory=factory(context,seen,'duplicate_ids'),
        key_provider=lambda:'fabricated-private-key',now=now)
    original_path=Path(approval.spec.output_dir)/(original['operation_key']+'.json')
    recovered,_=recover_positional_ids(original_path,budget_path=tmp_path/'ledger/budget.sqlite')
    monkeypatch.setattr('pdf_notion_mvp.lesson_generation.selected_key',lambda *a,**k:pytest.fail('cached recovery key lookup'))
    monkeypatch.setattr(study_session,'generate',lambda session,approval,expected_sha:generate(session,approval,expected_sha,now=now))
    main(['generate','--session-dir',str(tmp_path/'session'),'--approval',str(ap),'--expected-plan-sha256',approval.plan_sha256])
    packet=json.loads(capsys.readouterr().out)
    assert packet['status']=='unchanged' and packet['result_path'].endswith('-ids.json')
    assert json.loads(Path(packet['result_path']).read_text())==recovered
    assert json.loads(original_path.read_text())['status']=='failed_content_validation' and len(seen)==1
