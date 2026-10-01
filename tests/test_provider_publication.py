import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.live_run import (FILES, CompletedRunReceipt, RunCompletion, create_proposal,
    execute_approved, fingerprint)
from pdf_notion_mvp.notion_quiz import MemoryPageGateway, SectionPage, paragraph, plan_toggles
from pdf_notion_mvp.provider_publication import plan_provider_toggles, publish_provider_mock

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def completed(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    root = tmp_path
    (root/"fixtures").mkdir()
    for name in FILES:
        shutil.copyfile(Path(__file__).parents[1]/"fixtures"/name,root/"fixtures"/name)
    approval = create_proposal(root,model="gemini-3.1-flash-lite",budget_usd=Decimal("0.37"),now=NOW)
    for field in ("user_approved","data_transfer_confirmed","pricing_and_limits_confirmed",
                  "model_capabilities_confirmed","conditional_cost_understood"):
        setattr(approval,field,True)
    seen=[]
    def respond(request):
        seen.append(request)
        return httpx.Response(200,json={"candidates":[{"content":{"role":"model","parts":[
            {"text":(root/"fixtures/synthetic-quiz.json").read_text()}]},"finishReason":"STOP"}]})
    def factory(**kwargs):
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs["client_args"]["transport"]=httpx.MockTransport(respond)
        return ChatGoogleGenerativeAI(**kwargs)
    data=execute_approved(approval,root,expected_plan_sha256=approval.plan_sha256,
        client_factory=factory,key_provider=lambda:"fabricated-provider-publication-key",now=NOW)
    completion=RunCompletion.model_validate(data)
    binding=SectionPage(hub_id="00000000-0000-4000-8000-000000000001",
        page_id="00000000-0000-4000-8000-000000000002",document_id="synthetic-training",
        version="fixture-v1",section_id="unit.part")
    assert len(seen)==1 and completion.status=="ready_for_review"
    return root,approval,completion,binding,seen


def strings(block):
    data=block[block["type"]]
    content=[r["text"]["content"] for r in data["rich_text"]]
    for child in data.get("children",[]): content.extend(strings(child))
    return content


def ledger_path(root,completion):
    return root/"output/live-runs"/(str(completion.run_id)+".json")


def update_receipt_digest(root,completion):
    # Simulate a damaged trusted local ledger to prove content revalidation is independent.
    path=ledger_path(root,completion)
    receipt=CompletedRunReceipt.model_validate_json(path.read_text())
    receipt.completion_sha256=fingerprint(completion.model_dump(mode="json"))
    path.write_text(receipt.model_dump_json())


def test_fake_sdk_generation_validation_plan_and_idempotent_mock_publication(completed):
    root,approval,completion,binding,seen=completed
    immutable=[approval.model_dump(),completion.model_dump(),binding.model_dump()]
    plan=plan_provider_toggles(root,approval,completion,binding)
    assert plan.mode=="provider_review_only" and plan.provider_mode=="gemini"
    assert plan.execution_mode=="injected" and plan.model=="gemini-3.1-flash-lite"
    assert plan.human_review_required and not plan.semantic_correctness_verified and not plan.provider_attestation_verified
    text="\n".join(strings(plan.operations[0].block))
    assert "주입 실행 · 검토 필요" in text and "실제 외부 호출 여부" in text
    for part in ('"block_id": "b7"','"page": 2','"bbox"','"version": "fixture-v1"',
                 '"provider": "gemini"','"execution_mode": "injected"',"정답","해설과 원문 출처"):
        assert part in text
    notes=[paragraph("사용자 메모와 원문 출처를 보존")]
    gateway=MemoryPageGateway(binding,notes)
    first=publish_provider_mock(root,approval,completion,binding,gateway)
    gateway.children.append(paragraph("사용자의 새 풀이 메모"))
    saved=deepcopy(gateway.children)
    second=publish_provider_mock(root,approval,completion,binding,gateway)
    assert first.status=="mock_written" and second.status=="unchanged" and gateway.append_calls==1
    assert gateway.children==saved and gateway.children[:1]==notes and len(seen)==1
    assert immutable==[approval.model_dump(),completion.model_dump(),binding.model_dump()]


@pytest.mark.parametrize("change",["provider","nested_provider","execution_mode","status","nested_status",
    "request_count","attempts","events","source_digest","evidence_digest","document","version",
    "section","quote","claim","run_id","plan_sha","approval_flags","approval_spec","no_receipt",
    "reserved_only","receipt_provider","receipt_model","receipt_mode","receipt_digest",
    "human_review_flag","semantic_flag","provider_attestation_flag","usage_attestation_flag","billing_attestation_flag"])
def test_forged_or_incomplete_result_never_reaches_gateway(completed,change):
    from uuid import UUID
    root,approval,completion,binding,_=completed
    if change=="provider": completion.provider_mode="openai"
    elif change=="nested_provider": completion.result.provider_mode="mock"
    elif change=="execution_mode": completion.execution_mode="network_unattested"
    elif change=="status": completion.status="failed_human_review"
    elif change=="nested_status": completion.result.status="failed_human_review"
    elif change=="request_count": completion.request_count=0
    elif change=="attempts": completion.result.attempts=2
    elif change=="events": completion.result.events=["generate"]
    elif change in {"source_digest","evidence_digest"}: setattr(completion.result,change,"0"*64)
    elif change in {"document","version","section"}: setattr(completion.result,{"document":"document_id","section":"section_id"}.get(change,change),"wrong")
    elif change=="quote": completion.result.batch.questions[0].source_quote="unsupported source"
    elif change=="claim": completion.result.batch.questions[0].explanation="unsupported factual claim"
    elif change=="run_id": completion.run_id=UUID("00000000-0000-4000-8000-000000000099")
    elif change=="plan_sha": completion.plan_sha256="0"*64
    elif change=="approval_flags": approval.data_transfer_confirmed=False
    elif change=="approval_spec": approval.spec.max_output_tokens=256
    elif change=="human_review_flag": completion.result.human_review_required=False
    elif change=="semantic_flag": completion.result.semantic_correctness_verified=True
    elif change=="provider_attestation_flag": completion.provider_attestation_verified=True
    elif change=="usage_attestation_flag": completion.provider_usage_verified=True
    elif change=="billing_attestation_flag": completion.provider_billed_cost_verified=True
    elif change=="no_receipt": ledger_path(root,completion).unlink()
    elif change=="reserved_only": ledger_path(root,completion).write_text(json.dumps({"state":"consumed_before_key_lookup"}))
    else:
        path=ledger_path(root,completion);receipt=CompletedRunReceipt.model_validate_json(path.read_text())
        setattr(receipt,{"receipt_provider":"provider","receipt_model":"model","receipt_mode":"execution_mode","receipt_digest":"completion_sha256"}[change],
            {"receipt_provider":"openai","receipt_model":"unapproved-model","receipt_mode":"network_unattested","receipt_digest":"0"*64}[change])
        path.write_text(receipt.model_dump_json())
    class ForbiddenGateway:
        mode="mock"
        def snapshot(self,binding): pytest.fail("invalid input must stop before gateway read")
        def append(self,binding,block): pytest.fail("invalid input must stop before append")
    with pytest.raises(ValueError): publish_provider_mock(root,approval,completion,binding,ForbiddenGateway())


@pytest.mark.parametrize("change",["quote","answer","explanation","question","reference","count","errors","events"])
def test_source_verifier_still_blocks_if_receipt_digest_is_rewritten(completed,change):
    root,approval,completion,binding,_=completed
    q=completion.result.batch.questions[0]
    if change=="quote": q.source_quote="unsupported"
    elif change=="answer": q.answer="not in quote"
    elif change=="explanation": q.explanation="unverified claim"
    elif change=="question": q.question="unverified claim"
    elif change=="reference": q.source_block_ids=["unknown"]
    elif change=="count": completion.result.batch.questions.append(q.model_copy(deep=True))
    elif change=="errors": completion.result.errors=["failed_validation"]
    else: completion.result.events=["generate","self_validate"]
    update_receipt_digest(root,completion)
    with pytest.raises(ValueError): plan_provider_toggles(root,approval,completion,binding)


@pytest.mark.parametrize("file",FILES)
def test_changed_source_files_cannot_reuse_a_completed_run(completed,file):
    root,approval,completion,binding,_=completed
    path=root/"fixtures"/file
    path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises(ValueError,match="source"): plan_provider_toggles(root,approval,completion,binding)


def test_reserved_run_is_not_reused_after_publication_failure(completed):
    root,approval,completion,binding,seen=completed
    class TimeoutAfterAppend(MemoryPageGateway):
        def append(self,binding,block):
            super().append(binding,block)
            raise TimeoutError("fabricated-provider-publication-key")
    gateway=TimeoutAfterAppend(binding)
    first=publish_provider_mock(root,approval,completion,binding,gateway)
    second=publish_provider_mock(root,approval,completion,binding,gateway)
    assert first.status=="failed_human_review" and second.status=="unchanged"
    assert gateway.append_calls==1 and len(seen)==1


def test_live_notion_gateway_remains_disabled(completed):
    root,approval,completion,binding,_=completed
    class Live:
        mode="live"
        def snapshot(self,binding): pytest.fail("no actual Notion read")
        def append(self,binding,block): pytest.fail("no actual Notion write")
    receipt=publish_provider_mock(root,approval,completion,binding,Live())
    assert receipt.status=="failed_human_review"


def test_expired_generation_approval_can_review_completed_result_without_new_call(completed):
    # NOW is fixed in the past; current wall clock is beyond this fake approval's expiry.
    root,approval,completion,binding,seen=completed
    assert plan_provider_toggles(root,approval,completion,binding).operations
    assert len(seen)==1


def test_raw_provider_quiz_cannot_bypass_old_mock_boundary(completed):
    from pdf_notion_mvp.live_run import load_inputs
    root,approval,completion,binding,_=completed
    source,hierarchy,layer,*_=load_inputs(root)
    with pytest.raises(ValueError): plan_toggles(source,hierarchy,layer,completion.result,binding)


def test_cli_reads_completed_fake_run_and_persists_mock_page_across_restarts(completed,capsys):
    from pdf_notion_mvp.provider_publication_cli import main
    root,approval,completion,binding,seen=completed
    (root/"output/approval.json").write_text(approval.model_dump_json())
    (root/"output/result.json").write_text(completion.model_dump_json())
    (root/"output/binding.json").write_text(binding.model_dump_json())
    args=["--approval","output/approval.json","--result","output/result.json","--binding","output/binding.json"]
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    assert exc.value.code==0 and not (root/"output/mock-provider-page.json").exists()
    assert json.loads((root/"output/provider-notion-plan.json").read_text())["status"]=="plan_prepared"
    with pytest.raises(SystemExit) as exc: main(args+["--mock-publish"],project_root=root)
    assert exc.value.code==0
    page=root/"output/mock-provider-page.json"
    first=page.read_bytes()
    with pytest.raises(SystemExit) as exc: main(args+["--mock-publish"],project_root=root)
    assert exc.value.code==0 and page.read_bytes()==first and len(seen)==1
    result=json.loads((root/"output/provider-notion-plan.json").read_text())
    assert result["status"]=="unchanged" and result["receipt"]["human_review_required"]
    assert not result["model_network"] and not result["notion_network"]
    assert "fabricated-provider-publication-key" not in capsys.readouterr().out


@pytest.mark.parametrize("change",["output_input_alias","page_input_alias","output_page_alias","symlink","incomplete_page","wrong_binding"])
def test_cli_rejects_unsafe_paths_and_page_scope_without_changes(completed,change):
    from pdf_notion_mvp.provider_publication_cli import main
    from pdf_notion_mvp.notion_quiz import PageSnapshot
    root,approval,completion,binding,_=completed
    (root/"output/approval.json").write_text(approval.model_dump_json())
    (root/"output/result.json").write_text(completion.model_dump_json())
    (root/"output/binding.json").write_text(binding.model_dump_json())
    args=["--approval","output/approval.json","--result","output/result.json","--binding","output/binding.json","--mock-publish"]
    if change=="output_input_alias": args += ["--output","output/result.json"]
    elif change=="page_input_alias": args += ["--mock-page","output/result.json"]
    elif change=="output_page_alias": args += ["--output","output/mock-provider-page.json"]
    elif change=="symlink":
        (root/"output/linked.json").symlink_to(root/"output/result.json")
        args += ["--output","output/linked.json"]
    else:
        snapshot=PageSnapshot(binding=binding,complete=change!="incomplete_page",children=[paragraph("보존")])
        if change=="wrong_binding": snapshot.binding.section_id="wrong"
        (root/"output/mock-provider-page.json").write_text(snapshot.model_dump_json())
    before={p.name:p.read_bytes() for p in (root/"output").glob("*.json")}
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    assert exc.value.code==1
    assert before=={p.name:p.read_bytes() for p in (root/"output").glob("*.json")}


def test_second_validated_run_question_id_change_does_not_duplicate(completed):
    root,approval,completion,binding,seen=completed
    second_approval=create_proposal(root,model="gemini-3.1-flash-lite",budget_usd=Decimal("0.37"),now=NOW)
    for field in ("user_approved","data_transfer_confirmed","pricing_and_limits_confirmed",
                  "model_capabilities_confirmed","conditional_cost_understood"):
        setattr(second_approval,field,True)
    response=json.loads((root/"fixtures/synthetic-quiz.json").read_text())
    response["questions"][0]["question_id"]="new-generator-id"
    def respond(request):
        seen.append(request)
        return httpx.Response(200,json={"candidates":[{"content":{"role":"model","parts":[
            {"text":json.dumps(response,ensure_ascii=False)}]},"finishReason":"STOP"}]})
    def factory(**kwargs):
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs["client_args"]["transport"]=httpx.MockTransport(respond)
        return ChatGoogleGenerativeAI(**kwargs)
    second=RunCompletion.model_validate(execute_approved(second_approval,root,
        expected_plan_sha256=second_approval.plan_sha256,client_factory=factory,
        key_provider=lambda:"fabricated-provider-publication-key",now=NOW))
    first_plan=plan_provider_toggles(root,approval,completion,binding)
    second_plan=plan_provider_toggles(root,second_approval,second,binding)
    assert first_plan.completion_sha256!=second_plan.completion_sha256
    assert first_plan.operations==second_plan.operations
    gateway=MemoryPageGateway(binding)
    assert publish_provider_mock(root,approval,completion,binding,gateway).status=="mock_written"
    assert publish_provider_mock(root,second_approval,second,binding,gateway).status=="unchanged"
    assert gateway.append_calls==1 and len(seen)==2


def test_post_generation_receipt_failure_keeps_reservation_and_blocks_replay(completed,monkeypatch):
    from pdf_notion_mvp import live_run
    from pdf_notion_mvp.quiz import GenerationBlocked
    root,_,_,_,seen=completed
    approval=create_proposal(root,model="gemini-3.1-flash-lite",budget_usd=Decimal("0.37"),now=NOW)
    for field in ("user_approved","data_transfer_confirmed","pricing_and_limits_confirmed",
                  "model_capabilities_confirmed","conditional_cost_understood"):
        setattr(approval,field,True)
    def factory(**kwargs):
        from langchain_google_genai import ChatGoogleGenerativeAI
        def respond(request):
            seen.append(request)
            return httpx.Response(200,json={"candidates":[{"content":{"role":"model","parts":[
                {"text":(root/"fixtures/synthetic-quiz.json").read_text()}]},"finishReason":"STOP"}]})
        kwargs["client_args"]["transport"]=httpx.MockTransport(respond)
        return ChatGoogleGenerativeAI(**kwargs)
    def fail(*args): raise OSError("fabricated-provider-publication-key")
    monkeypatch.setattr(live_run,"save_completion_receipt",fail)
    with pytest.raises(OSError): execute_approved(approval,root,expected_plan_sha256=approval.plan_sha256,
        client_factory=factory,key_provider=lambda:"fabricated-provider-publication-key",now=NOW)
    with pytest.raises(GenerationBlocked,match="already reserved"):
        execute_approved(approval,root,expected_plan_sha256=approval.plan_sha256,now=NOW,
            key_provider=lambda:pytest.fail("failed receipt must not replay generation"))
    assert len(seen)==2
    reserved=json.loads((root/"output/live-runs"/(str(approval.spec.run_id)+".json")).read_text())
    assert reserved["state"]=="consumed_before_key_lookup"


@pytest.mark.parametrize("failure",["partial_write","replace"])
def test_mock_page_persistence_failure_keeps_notes_and_normal_restart(completed,monkeypatch,capsys,failure):
    import tempfile
    from pdf_notion_mvp.provider_publication_cli import main
    from pdf_notion_mvp.notion_quiz import PageSnapshot
    root,approval,completion,binding,_=completed
    for name,value in [("approval.json",approval),("result.json",completion),("binding.json",binding)]:
        (root/"output"/name).write_text(value.model_dump_json())
    page=root/"output/atomic-page.json"
    initial=MemoryPageGateway(binding,[paragraph("손상 없이 보존할 사용자 메모"),paragraph("기존 원문 출처")]).snapshot(binding)
    page.write_text(initial.model_dump_json(indent=2))
    original=page.read_bytes()
    args=["--approval","output/approval.json","--result","output/result.json","--binding","output/binding.json",
          "--mock-publish","--mock-page","output/atomic-page.json","--output","output/atomic-plan.json"]
    original_write=Path.write_text
    original_replace=Path.replace
    original_temporary=tempfile.NamedTemporaryFile
    class InterruptedWriter:
        def __init__(self,stream): self.stream=stream
        def __getattr__(self,name): return getattr(self.stream,name)
        def __enter__(self): self.stream.__enter__();return self
        def __exit__(self,*args): return self.stream.__exit__(*args)
        def write(self,text):
            self.stream.write(text.encode()[:80].decode(errors="ignore"))
            self.stream.flush()
            raise OSError("fabricated-secret-must-not-be-logged")
    def partial_old_write(path,text,*args,**kwargs):
        if path==page:
            original_write(path,text.encode()[:80].decode(errors="ignore"),*args,**kwargs)
            raise OSError("fabricated-secret-must-not-be-logged")
        return original_write(path,text,*args,**kwargs)
    def partial_new_write(*args,**kwargs):
        stream=original_temporary(*args,**kwargs)
        if Path(stream.name).parent==page.parent:
            return InterruptedWriter(stream)
        return stream
    def fail_replace(path,target):
        if Path(target)==page: raise OSError("fabricated-secret-must-not-be-logged")
        return original_replace(path,target)
    with monkeypatch.context() as scoped:
        if failure=="partial_write":
            scoped.setattr(Path,"write_text",partial_old_write)
            scoped.setattr(tempfile,"NamedTemporaryFile",partial_new_write)
        else: scoped.setattr(Path,"replace",fail_replace)
        with pytest.raises(SystemExit) as exc: main(args,project_root=root)
        assert exc.value.code==1
    assert page.read_bytes()==original
    assert PageSnapshot.model_validate_json(page.read_text())==initial
    assert not list(page.parent.glob("."+page.name+"-*"))
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    assert exc.value.code==0
    saved=page.read_bytes()
    assert PageSnapshot.model_validate_json(page.read_text()).children[:2]==initial.children
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    assert exc.value.code==0 and page.read_bytes()==saved
    assert json.loads((root/"output/atomic-plan.json").read_text())["status"]=="unchanged"
    assert "fabricated-secret-must-not-be-logged" not in capsys.readouterr().out
