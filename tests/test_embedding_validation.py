import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.embedding_validation import (
    ENDPOINT, REQUEST_COST_MICRO_USD, create_proposal, execute, request_bodies, vector,
)
from pdf_notion_mvp.lesson_generation import _budget_connection
from pdf_notion_mvp.live_run import fingerprint
from pdf_notion_mvp.rag import prepare_index
from pdf_notion_mvp.review import HierarchicalOutline, OutlineNode, ReviewLayer, document_digest


@pytest.fixture
def proposal(tmp_path,source):
    # Authored synthetic inputs exercise the same source-bound adapter, no real key.
    h=HierarchicalOutline(document_id=source.document.document_id,version=source.document.version,nodes=[
        OutlineNode(node_id="one",title="one",source_pages=[1],block_ids=[b.block_id for b in source.document.blocks if b.source.page==1],review_status="confirmed"),
        OutlineNode(node_id="two",title="two",source_pages=[2],block_ids=[b.block_id for b in source.document.blocks if b.source.page==2],review_status="confirmed")])
    r=ReviewLayer(document_id=source.document.document_id,version=source.document.version,
                  source_digest=document_digest(source.document))
    entries=prepare_index(source,h,r).entries
    manifest=[dict(evidence_id=e.evidence_id,source_block_id=e.source_block_id,page=e.source.page,text=e.text) for e in entries]
    assert len(manifest)==2
    paths=[]; inputs=tmp_path/"inputs"; inputs.mkdir()
    for name,value in [("source",source.model_dump(mode="json")),("outline",h.model_dump(mode="json")),("review",r.model_dump(mode="json")),("manifest",manifest)]:
        p=inputs/f"{name}.json";p.write_text(json.dumps(value,ensure_ascii=False));paths.append(p)
    budget=tmp_path/"ledger.sqlite";db=_budget_connection(budget)
    db.execute("INSERT INTO reservations(operation,run_id,cost,status,request_count) VALUES('legacy','legacy',9011200,'completed',25)")
    db.close()
    now=datetime.now(timezone.utc)
    p=create_proposal(paths,["체크포인트 재개"],tmp_path/"private-vectors",budget,tmp_path,checked_on=now.date(),now=now)
    p.user_approved=p.data_transfer_confirmed=p.pricing_capabilities_confirmed=True
    return p


def factory(handler):
    return lambda **kwargs:httpx.Client(transport=httpx.MockTransport(handler),**kwargs)


def packet(n=0):
    values=[0.0]*768;values[n]=1.0
    return {"embedding":{"values":values},"usageMetadata":{"promptTokenCount":10}}


def forbidden():
    pytest.fail("key lookup or transport must not occur")


def test_shared_budget_before_key_one_content_receipts_and_repeat(proposal):
    requests=[];keys=[]
    def key():
        db=sqlite3.connect(proposal.spec.budget_ledger)
        assert db.execute("SELECT sum(cost) FROM reservations").fetchone()[0]==9011200+3*REQUEST_COST_MICRO_USD
        assert db.execute("SELECT count(*) FROM reservations WHERE status='reserved'").fetchone()[0]==3
        db.close();keys.append(True);return "fake-key"
    def respond(request):
        body=json.loads(request.content)
        assert request.url==ENDPOINT and len(body["content"]["parts"])==1
        assert set(body)=={"content","model","outputDimensionality"}
        assert request.headers["x-goog-api-key"]=="fake-key"
        requests.append(body)
        return httpx.Response(200,json=packet(len(requests)%2))
    result,state=execute(proposal,proposal.plan_sha256,client_factory=factory(respond),key_provider=key)
    assert state=="completed" and len(requests)==3 and len(keys)==1
    assert result["documents"]==2 and result["dimensions"]==768 and len(result["rankings"])==1
    assert result["rankings"][0]["similarity_is_not_answer_evidence"]
    for path in result["provider_packet_paths"]:
        saved=json.loads(Path(path).read_text());assert saved["http_status"]==200
        assert "fake-key" not in Path(path).read_text()
    again,state=execute(proposal,proposal.plan_sha256,client_factory=factory(forbidden),key_provider=forbidden)
    assert state=="unchanged" and again["request_count_this_run"]==0


@pytest.mark.parametrize("change",["approval","sha","input","query","expired","price"])
def test_invalid_plan_blocks_before_key_and_reservation(proposal,change):
    if change=="approval": proposal.user_approved=False
    if change=="sha": proposal.plan_sha256="0"*64
    if change=="input":
        p=Path(proposal.spec.input_paths[3]);d=json.loads(p.read_text());d[0]["text"]="changed";p.write_text(json.dumps(d))
    if change=="query": proposal.spec.queries=["outside approval"]
    if change=="expired":
        proposal.spec.created_at-=timedelta(days=2);proposal.spec.expires_at-=timedelta(days=2)
        proposal.plan_sha256=fingerprint(proposal.spec.model_dump(mode="json"))
    if change=="price":
        proposal.spec.snapshot.checked_on-=timedelta(days=2)
        proposal.plan_sha256=fingerprint(proposal.spec.model_dump(mode="json"))
    with pytest.raises(ValueError): execute(proposal,proposal.plan_sha256,key_provider=forbidden)
    db=sqlite3.connect(proposal.spec.budget_ledger)
    assert db.execute("SELECT count(*) FROM reservations").fetchone()[0]==1;db.close()


def test_shared_cap_blocks_before_key(proposal):
    db=sqlite3.connect(proposal.spec.budget_ledger)
    db.execute("UPDATE reservations SET cost=9999999");db.commit();db.close()
    with pytest.raises(ValueError): execute(proposal,proposal.plan_sha256,key_provider=forbidden)


@pytest.mark.parametrize("change",["delete","replace","erase","reduce"])
def test_existing_budget_cannot_reset_after_approval(proposal,change):
    path=Path(proposal.spec.budget_ledger)
    if change=="delete": path.unlink()
    elif change=="replace":
        alternate=path.with_name("alternate.sqlite")
        db=_budget_connection(alternate);db.close();alternate.replace(path)
    else:
        db=sqlite3.connect(path)
        db.execute("DELETE FROM reservations" if change=="erase" else "UPDATE reservations SET cost=1")
        db.commit();db.close()
    with pytest.raises(ValueError): execute(proposal,proposal.plan_sha256,key_provider=forbidden)
    if change=="delete": assert not path.exists()


@pytest.mark.parametrize("failure",["timeout","http","dimension","nonfinite","zero","oversized"])
def test_failed_or_uncertain_call_stays_consumed_no_retry(proposal,failure):
    calls=[]
    def fail(request):
        calls.append(True)
        if failure=="timeout": raise httpx.ReadTimeout("secret-fake-key-private-body",request=request)
        if failure=="http": return httpx.Response(429,json={"error":{"message":"private-body"}})
        p=packet()
        if failure=="dimension": p["embedding"]["values"].pop()
        if failure=="nonfinite": return httpx.Response(200,content=b'{"embedding":{"values":[NaN]}}')
        if failure=="zero": p["embedding"]["values"]=[0]*768
        if failure=="oversized": return httpx.Response(200,content=b"x"*1000001)
        return httpx.Response(200,json=p)
    with pytest.raises(ValueError) as exc:
        execute(proposal,proposal.plan_sha256,client_factory=factory(fail),key_provider=lambda:"fake-key")
    assert "private-body" not in str(exc.value) and "fake-key" not in str(exc.value)
    assert len(calls)==1
    db=sqlite3.connect(proposal.spec.budget_ledger)
    assert db.execute("SELECT sum(cost) FROM reservations").fetchone()[0]==9011200+3*REQUEST_COST_MICRO_USD
    assert db.execute("SELECT sum(request_count) FROM reservations WHERE operation!='legacy'").fetchone()[0]==1
    db.close()
    with pytest.raises(ValueError): execute(proposal,proposal.plan_sha256,client_factory=factory(forbidden),key_provider=forbidden)


def test_changed_saved_packet_blocks_before_key(proposal):
    result,_=execute(proposal,proposal.plan_sha256,client_factory=factory(lambda request:httpx.Response(200,json=packet())),key_provider=lambda:"fake-key")
    path=Path(result["provider_packet_paths"][0]);data=json.loads(path.read_text());data["provider_packet"]=packet(1);path.write_text(json.dumps(data))
    with pytest.raises(ValueError): execute(proposal,proposal.plan_sha256,client_factory=factory(forbidden),key_provider=forbidden)


def test_json_escaped_key_echo_never_reaches_receipt(proposal):
    def echo(request):
        return httpx.Response(429,content=b'{"error":{"message":"\\u0066abricated","nested":["fabricated"],"\\u0066abricated":"echo"}}')
    with pytest.raises(ValueError): execute(proposal,proposal.plan_sha256,client_factory=factory(echo),key_provider=lambda:"fabricated")
    saved=list(Path(proposal.spec.output_dir).glob("*.json"))
    assert len(saved)==1
    assert "fabricated" not in saved[0].read_text()


def test_json_formatting_cannot_repeat_semantically_identical_requests(proposal):
    execute(proposal,proposal.plan_sha256,client_factory=factory(lambda request:httpx.Response(200,json=packet())),key_provider=lambda:"fake-key")
    manifest=Path(proposal.spec.input_paths[-1]);manifest.write_text(json.dumps(json.loads(manifest.read_text()),indent=4))
    new=create_proposal(proposal.spec.input_paths,proposal.spec.queries,proposal.spec.output_dir,
        proposal.spec.budget_ledger,proposal.spec.key_project_root,checked_on=datetime.now(timezone.utc).date())
    new.user_approved=new.data_transfer_confirmed=new.pricing_capabilities_confirmed=True
    assert new.spec.input_sha256!=proposal.spec.input_sha256
    assert new.spec.request_digest==proposal.spec.request_digest and new.spec.corpus_digest==proposal.spec.corpus_digest
    result,state=execute(new,new.plan_sha256,client_factory=factory(forbidden),key_provider=forbidden)
    assert state=="unchanged" and result["request_count_this_run"]==0


def test_duplicate_input_and_fragment_manifest_are_rejected(proposal):
    paths=list(map(Path,proposal.spec.input_paths));manifest=json.loads(paths[-1].read_text())
    manifest.append(manifest[0]);paths[-1].write_text(json.dumps(manifest))
    with pytest.raises(ValueError): create_proposal(paths,["query"],proposal.spec.output_dir,proposal.spec.budget_ledger,
        proposal.spec.key_project_root,checked_on=datetime.now(timezone.utc).date())


def test_plaintext_retrieval_prefixes_do_not_add_other_data():
    b=request_bodies([dict(text="approved")],["approved query"])
    assert b[0]["content"]["parts"]==[{"text":"title: none | text: approved"}]
    assert b[1]["content"]["parts"]==[{"text":"task: search result | query: approved query"}]
    with pytest.raises(ValueError): vector({"embedding":{"values":[True]*768}})
