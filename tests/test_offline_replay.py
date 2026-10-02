import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_document_lesson import approval
from pdf_notion_mvp.document_lesson import BatchDraft
from pdf_notion_mvp.document_study import item_ids
from pdf_notion_mvp.live_run import fingerprint
from pdf_notion_mvp.offline_replay import CachedBatch, ReplaySpec, SQLiteMockGateway, run_replay
from pdf_notion_mvp.study_bundle import sha


@pytest.fixture
def spec(approval,monkeypatch):
    # Authored test PDF placeholder; test inspector is explicitly mocked.
    paths=[Path(p) for p in approval.spec.input_paths]
    pdf=paths[0].parent/'input.pdf';pdf.write_bytes(b'authored fake PDF boundary')
    source=json.loads(paths[0].read_text());old=source['document']['version'];version=sha(pdf.read_bytes())
    def replace(v):
        if isinstance(v,dict):return {k:replace(x) for k,x in v.items()}
        if isinstance(v,list):return [replace(x) for x in v]
        return version if v==old else v
    for path in paths:path.write_text(json.dumps(replace(json.loads(path.read_text()))))
    from pdf_notion_mvp.contracts import DocumentIR
    from pdf_notion_mvp.review import document_digest
    layer=json.loads(paths[2].read_text());layer['source_digest']=document_digest(DocumentIR.model_validate(json.loads(paths[0].read_text())['document']));paths[2].write_text(json.dumps(layer))
    from pdf_notion_mvp.document_lesson import prepare
    from test_document_lesson import NOW
    a=prepare(paths,[1,2],output_dir=Path(approval.spec.output_dir),budget_path=Path(approval.spec.budget_ledger),key_project_root=pdf.parent,now=NOW)
    for key in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(a,key,True)
    import httpx
    from test_document_lesson import draft
    def factory(**kwargs):
        def response(request):
            from pdf_notion_mvp.document_lesson import validate_approval
            _,context,_=validate_approval(a,a.plan_sha256,NOW);value=draft(context)
            value['pages'].append(dict(page=2,title='다음 단계',notes=[],exercises=[],uncertainty='합성 검수'))
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':json.dumps(value)}]},'finishReason':'STOP'}]})
        return httpx.Client(transport=httpx.MockTransport(response),**kwargs)
    from pdf_notion_mvp.document_lesson import execute
    result,_=execute(a,a.plan_sha256,client_factory=factory,key_provider=lambda:'fabricated-key',now=NOW)
    path=next(p for p in Path(a.spec.output_dir).glob('*.json') if not p.name.endswith('-provider.json'))
    review=path.parent/'review.json';review.write_text(json.dumps(dict(result_digest=fingerprint(result),reviewed_ids=item_ids(BatchDraft.model_validate(result['draft'])),reviewer='independent authored test',decision='accepted',notes=[],review_kind='independent_source_comparison')))
    monkeypatch.setattr('pdf_notion_mvp.offline_replay.inspect_pdf',lambda _:SimpleNamespace(sha256=version,page_count=2))
    return ReplaySpec(pdf_path=pdf,source_path=paths[0],outline_path=paths[1],review_path=paths[2],budget_path=a.spec.budget_ledger,batches=[CachedBatch(result_path=path,review_path=review)])


def test_end_to_end_resume_reads_storage_and_preserves_budget_and_memo(spec,tmp_path):
    output=tmp_path/'replay';before=spec.budget_path.read_bytes()
    first=run_replay(spec,output_dir=output);assert first['mock_creates_this_invocation']==1
    with sqlite3.connect(output/'mock-pages.sqlite') as db:db.execute("UPDATE pages SET memo='작성한 개인 메모'")
    second=run_replay(spec,output_dir=output);assert second['mock_creates_this_invocation']==0
    assert second['source_pages']==2 and second['mock_pages_verified']==1 and first['run_key']==second['run_key']
    assert before==spec.budget_path.read_bytes() and second['actual_model_calls']==second['actual_notion_calls']==0
    with sqlite3.connect(output/'mock-pages.sqlite') as db:assert db.execute('SELECT memo FROM pages').fetchone()[0]=='작성한 개인 메모'


def test_committed_create_timeout_recovers_without_second_create(spec,tmp_path):
    class TimeoutAfterCommit(SQLiteMockGateway):
        calls=0
        def create(self,*args):
            self.calls+=1;super().create(*args);raise TimeoutError('simulated lost response')
    output=tmp_path/'replay';gateway=TimeoutAfterCommit(output/'mock-pages.sqlite')
    with pytest.raises(TimeoutError):run_replay(spec,output_dir=output,gateway=gateway)
    result=run_replay(spec,output_dir=output,gateway=gateway)
    assert gateway.calls==1 and result['mock_creates_this_invocation']==0 and result['mock_pages_verified']==1


@pytest.mark.parametrize('stop_stage',[1,2,3,4])
def test_restart_after_each_checkpoint(spec,tmp_path,stop_stage):
    def stop(stage):
        if stage==stop_stage:raise RuntimeError('interrupted')
    output=tmp_path/'replay'
    with pytest.raises(RuntimeError):run_replay(spec,output_dir=output,hook=stop)
    result=run_replay(spec,output_dir=output)
    assert result['status']=='completed_mock_replay'
    with sqlite3.connect(output/'mock-pages.sqlite') as db:assert db.execute('SELECT count(*) FROM pages').fetchone()[0]==1


@pytest.mark.parametrize('damage',['content','title'])
def test_post_completion_changes_are_not_hidden_by_ready_checkpoint(spec,tmp_path,damage):
    output=tmp_path/'replay';run_replay(spec,output_dir=output)
    with sqlite3.connect(output/'mock-pages.sqlite') as db:
        db.execute(f"UPDATE pages SET {damage}='changed'")
    with pytest.raises(ValueError,match='readback'):run_replay(spec,output_dir=output)
    with sqlite3.connect(output/'mock-pages.sqlite') as db:assert db.execute('SELECT count(*) FROM pages').fetchone()[0]==1


@pytest.mark.parametrize('damage',['review','pdf','duplicate','budget'])
def test_stale_cache_missing_coverage_and_budget_are_blocked_before_mock_write(spec,tmp_path,damage):
    if damage=='review':
        p=spec.batches[0].review_path;v=json.loads(p.read_text());v['decision']='needs_changes';p.write_text(json.dumps(v))
    elif damage=='pdf':spec.pdf_path.write_bytes(b'changed')
    elif damage=='duplicate':spec.batches.append(spec.batches[0])
    else:spec.budget_cap_micro_usd=0
    with pytest.raises(ValueError):run_replay(spec,output_dir=tmp_path/'replay')
    assert not (tmp_path/'replay/mock-pages.sqlite').exists()


def test_mock_gateway_is_durable_and_detects_db_replacement(tmp_path):
    g=SQLiteMockGateway(tmp_path/'gateway.sqlite');id=g.create('owner','title','content').page_id
    assert SQLiteMockGateway(tmp_path/'gateway.sqlite').fetch(id)['content']=='content'
    path=tmp_path/'gateway.sqlite';path.rename(tmp_path/'old.sqlite');SQLiteMockGateway(path)
    with pytest.raises(ValueError,match='changed'):g.find('owner')


def test_concurrent_bootstrap_and_create_have_one_owner_and_preserve_memo(tmp_path):
    path=tmp_path/'gateway.sqlite'
    def create(_):
        gateway=SQLiteMockGateway(path)
        return gateway.create('owner','title','content')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(create,range(8)))
    assert sum(r.created for r in results)==1 and len({r.page_id for r in results})==1
    with sqlite3.connect(path) as db:db.execute("UPDATE pages SET memo='memo'")
    assert not SQLiteMockGateway(path).create('owner','title','content').created
    assert SQLiteMockGateway(path).fetch(results[0].page_id)['user_memo']=='memo'


def test_unknown_database_and_same_inode_identity_change_are_blocked(tmp_path):
    unknown=tmp_path/'unknown.sqlite';unknown.write_bytes(b'')
    with pytest.raises(sqlite3.OperationalError):SQLiteMockGateway(unknown)
    assert unknown.read_bytes()==b''
    gateway=SQLiteMockGateway(tmp_path/'valid.sqlite')
    with sqlite3.connect(gateway.path) as db:db.execute("UPDATE identity SET value='changed'")
    with pytest.raises(ValueError,match='identity'):gateway.find('owner')


@pytest.mark.parametrize('stage',[1,2,3])
def test_changed_input_after_checkpoint_blocks_mock_write(spec,tmp_path,stage):
    def change(number):
        if number==stage:spec.outline_path.write_text('{}')
    with pytest.raises(ValueError,match='changed'):run_replay(spec,output_dir=tmp_path/'replay',hook=change)
    assert not (tmp_path/'replay/mock-pages.sqlite').exists()


def test_reviewed_reading_binding_and_duplicate_coverage_guard(spec,tmp_path):
    from pdf_notion_mvp.offline_replay import ReusedReading
    inputs=tmp_path/'reading-inputs';inputs.mkdir()
    root=inputs/'reading';root.mkdir()
    (root/'original.pdf').write_bytes(spec.pdf_path.read_bytes())
    (root/'models.html').write_text('<h1>확정 설명</h1>')
    (root/'manifest.json').write_text(json.dumps(dict(source_pdf_sha256=sha(spec.pdf_path.read_bytes()),modules=[dict(key='models',title='Model',file='models.html',source_pages=[2])])) )
    review=inputs/'reading-review.json'
    review.write_text(json.dumps(dict(decision='accepted',bound_reading_sha256={p.name:sha(p.read_bytes()) for p in root.iterdir()})))
    item=ReusedReading(root=root,review_path=review,module_key='models',pages=[2])
    from pdf_notion_mvp.offline_replay import reading_reuse
    assert reading_reuse(item,sha(spec.pdf_path.read_bytes()))['semantics_newly_confirmed'] is False
    # Retain accepted two-page cache; double coverage must reject publication.
    spec.reused_reading=[item]
    with pytest.raises(ValueError,match='exactly once'):run_replay(spec,output_dir=tmp_path/'replay')
    (root/'extra.txt').write_text('unreviewed')
    with pytest.raises(ValueError,match='file set'):reading_reuse(item,sha(spec.pdf_path.read_bytes()))
    (root/'extra.txt').unlink();(root/'models.html').write_text('changed')
    with pytest.raises(ValueError,match='changed'):reading_reuse(item,sha(spec.pdf_path.read_bytes()))
