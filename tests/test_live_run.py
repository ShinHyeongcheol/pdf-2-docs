import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.live_run import (FILES, RunApproval, artifact_path, cost_envelope,
    create_proposal, execute_approved, fingerprint)
from pdf_notion_mvp.quiz import GenerationBlocked

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
MODEL = "gemini-3.1-flash-lite"


@pytest.fixture
def project(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    origin = Path(__file__).parents[1] / "fixtures"
    for name in FILES:
        shutil.copyfile(origin/name, fixtures/name)
    return tmp_path


def proposal(project, **kwargs):
    return create_proposal(project, model=MODEL, budget_usd=Decimal("0.37"), now=NOW, **kwargs)


def approve(value):
    value = value.model_copy(deep=True)
    for field in ("user_approved", "data_transfer_confirmed", "pricing_and_limits_confirmed",
                  "model_capabilities_confirmed", "conditional_cost_understood"):
        setattr(value, field, True)
    return value


def resign(value):
    value.plan_sha256 = fingerprint(value.spec.model_dump(mode="json"))
    return value


def sdk_factory(project, seen, violation=None):
    from langchain_google_genai import ChatGoogleGenerativeAI
    def respond(request):
        seen.append(request)
        if violation == "timeout": raise httpx.ReadTimeout("fabricated-only-secret", request=request)
        if violation == "error": return httpx.Response(503, json={"error":{"code":503,"message":"fabricated-only-secret","status":"UNAVAILABLE"}})
        if violation == "refusal": return httpx.Response(200, json={"promptFeedback":{"blockReason":"SAFETY"}})
        text = "{" if violation == "json" else (project/"fixtures/synthetic-quiz.json").read_text()
        if violation == "claim":
            data = json.loads(text)
            data["questions"][0]["source_block_ids"] = ["unsupported"]
            text = json.dumps(data)
        return httpx.Response(200, json={"candidates":[{"content":{"role":"model","parts":[{"text":text}]},"finishReason":"STOP"}]})
    def factory(**kwargs):
        args = dict(kwargs["client_args"])
        args["transport"] = httpx.MockTransport(respond)
        if violation in {"contents", "tools", "output", "timeout", "schema", "extra"}:
            def change(request):
                if violation == "timeout": return  # Real timeout mock path, same contract.
                data = json.loads(request.content)
                if violation == "contents": data["contents"][0]["parts"][0]["text"] += "unapproved data"
                elif violation == "tools": data["tools"] = [{"googleSearch":{}}]
                elif violation == "output": data["generationConfig"]["maxOutputTokens"] = 4096
                elif violation == "schema": data["generationConfig"]["responseJsonSchema"] = {}
                else: data["cachedContent"] = "unapproved-cache"
                request._content = json.dumps(data).encode()
            args["event_hooks"] = {"request":[change]+args["event_hooks"]["request"]}
        kwargs["client_args"] = args
        if violation == "host": kwargs["base_url"] = "https://other.example"
        return ChatGoogleGenerativeAI(**kwargs)
    return factory


def execute(project, approval, **kwargs):
    return execute_approved(approval, project, expected_plan_sha256=approval.plan_sha256,
        now=NOW, **kwargs)


def test_proposal_is_offline_not_an_approval(project):
    value = proposal(project)
    assert not value.user_approved and not value.data_transfer_confirmed
    assert value.spec.input_digests.keys() == set(FILES)
    assert value.spec.max_requests == value.spec.max_attempts == value.spec.max_questions == 1
    assert cost_envelope(value.spec) == Decimal("0.360448")
    assert value.spec.conservative_input_tokens == 1_048_576
    assert value.spec.conservative_billable_output_tokens == 65_536
    assert value.spec.max_output_tokens == 512
    assert value.spec.expires_at-value.spec.created_at == timedelta(hours=1)
    assert RunApproval.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("gate", ["user_approved", "data_transfer_confirmed", "pricing_and_limits_confirmed", "model_capabilities_confirmed", "conditional_cost_understood", "fingerprint", "source", "outline", "review", "expiry", "naive_time", "snapshot", "capacity", "output_capacity", "budget", "unknown_model", "request_digest", "too_small_bytes", "stale_price"])
def test_every_gate_blocks_before_key_client_or_reservation(project, gate):
    value = approve(proposal(project))
    if gate in RunApproval.model_fields and gate != "spec": setattr(value, gate, False)
    elif gate == "fingerprint": value.plan_sha256 = "0"*64
    elif gate in {"source", "outline", "review"}:
        name = {"source":FILES[0], "outline":FILES[1], "review":FILES[2]}[gate]
        path = project/"fixtures"/name
        path.write_bytes(path.read_bytes()+b" ")
    elif gate == "expiry": value.spec.expires_at = NOW
    elif gate == "naive_time": value.spec.created_at = NOW.replace(tzinfo=None)
    elif gate == "snapshot": value.spec.model_snapshot.input_usd_per_million = Decimal("0.01")
    elif gate == "capacity": value.spec.conservative_input_tokens = 2000
    elif gate == "output_capacity": value.spec.conservative_billable_output_tokens = 512
    elif gate == "budget": value.spec.budget_usd = Decimal("0.001")
    elif gate == "unknown_model":
        raw=value.model_dump(); raw["spec"]["model_snapshot"]["model"]="unknown"
        with pytest.raises(ValueError): RunApproval.model_validate(raw)
        return
    elif gate == "request_digest": value.spec.request_contract_digest = "1"*64
    elif gate == "too_small_bytes": value.spec.max_request_bytes = 1
    elif gate == "stale_price":
        value.spec.created_at += timedelta(days=8)
        value.spec.expires_at += timedelta(days=8)
    if gate != "fingerprint": resign(value)
    reads = []
    with pytest.raises(GenerationBlocked):
        execute_approved(value, project, expected_plan_sha256=value.plan_sha256,
            now=NOW+timedelta(days=8) if gate == "stale_price" else NOW,
            key_provider=lambda: reads.append("key") or "fabricated-only-secret",
            client_factory=lambda **kwargs: reads.append("client"))
    assert reads == [] and not (project/"output/live-runs").exists()


def test_actual_gemini_sdk_one_approved_request_fake_only(project, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    value = approve(proposal(project))
    seen, keys = [], []
    result = execute(project, value, client_factory=sdk_factory(project, seen),
        key_provider=lambda: keys.append(1) or "fabricated-only-secret")
    assert result["status"] == "ready_for_review" and result["request_count"] == 1
    assert result["result"]["attempts"] == 1 and len(seen) == len(keys) == 1
    assert len(result["result"]["batch"]["questions"]) == 1
    assert result["result"]["provider_mode"] == "gemini"
    assert not result["provider_billed_cost_verified"] and not result["provider_usage_verified"]
    assert seen[0].url.path == "/v1beta/models/"+MODEL+":generateContent"
    assert fingerprint(json.loads(seen[0].content)) == value.spec.request_contract_digest
    assert seen[0].extensions["timeout"] == {name:20 for name in ("connect", "read", "write", "pool")}
    with pytest.raises(GenerationBlocked, match="already reserved"):
        execute(project, value, key_provider=lambda: pytest.fail("second key read"))
    assert len(seen) == 1


@pytest.mark.parametrize("violation", ["timeout", "error", "refusal", "json", "claim", "contents", "tools", "output", "schema", "extra", "host"])
def test_fake_sdk_failure_never_retries_or_reuses_approval(project, monkeypatch, violation, caplog):
    import os
    monkeypatch.setattr(os, "environ", {})
    value = approve(proposal(project))
    seen = []
    result = execute(project, value, client_factory=sdk_factory(project, seen, violation),
        key_provider=lambda:"fabricated-only-secret")
    assert result["status"] == "failed_human_review" and result["result"]["attempts"] == 1
    assert len(seen) == (1 if violation in {"timeout", "error", "refusal", "json", "claim"} else 0)
    assert "fabricated-only-secret" not in json.dumps(result)+caplog.text
    with pytest.raises(GenerationBlocked, match="already reserved"):
        execute(project, value, key_provider=lambda: pytest.fail("second key read"))


def test_shared_approval_atomic_across_runs(project, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    value = approve(proposal(project))
    seen = []
    def start():
        try:
            return execute(project, value, client_factory=sdk_factory(project, seen),
                key_provider=lambda:"fabricated-only-secret")["status"]
        except GenerationBlocked:
            return "reserved"
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _:start(), range(2)))
    assert sorted(statuses) == ["ready_for_review", "reserved"] and len(seen) == 1


def test_missing_key_consumes_run_without_constructing_client(project):
    value = approve(proposal(project))
    result = execute(project, value, key_provider=lambda:None,
        client_factory=lambda **kwargs: pytest.fail("SDK must not be created"))
    assert result["status"] == "failed_human_review" and result["request_count"] == 0
    with pytest.raises(GenerationBlocked): execute(project, value)


def test_live_cli_end_to_end_plan_approval_execution_and_restart(project, monkeypatch, capsys):
    import os
    from pdf_notion_mvp.live_run_cli import main
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--model",MODEL,"--budget-usd","0.37"],project_root=project,now=NOW,
            key_provider=lambda:pytest.fail("offline plan must not read keys"))
    assert exc.value.code == 0
    plan = project/"output/live-approval-proposal.json"
    value = approve(RunApproval.model_validate_json(plan.read_text()))
    plan.write_text(value.model_dump_json())
    args=["--mode","live","--approval", "output/live-approval-proposal.json",
        "--expected-plan-sha256",value.plan_sha256]
    seen=[]
    with pytest.raises(SystemExit) as exc:
        main(args, project_root=project, now=NOW, client_factory=sdk_factory(project,seen),
            key_provider=lambda:"fabricated-only-secret")
    assert exc.value.code == 0 and len(seen) == 1
    result_path = project/"output"/(str(value.spec.run_id)+"-result.json")
    assert json.loads(result_path.read_text())["result"]["human_review_required"] is True
    # New output still cannot replay the consumed approval in a restarted CLI.
    with pytest.raises(SystemExit) as exc:
        main(args+["--output","output/restarted-result.json"],project_root=project,now=NOW,
            key_provider=lambda:pytest.fail("restart must not read keys"))
    assert exc.value.code == 1 and len(seen) == 1
    assert "fabricated-only-secret" not in capsys.readouterr().out


def test_default_mock_cli_never_reads_keys_or_approval(project):
    from pdf_notion_mvp.live_run_cli import main
    with pytest.raises(SystemExit) as exc:
        main([],project_root=project,key_provider=lambda:pytest.fail("default must not read keys"),
            client_factory=lambda **kwargs:pytest.fail("default must not construct SDK"))
    assert exc.value.code == 0
    result = json.loads((project/"output/mock-run.json").read_text())
    assert result["provider_mode"] == "mock" and result["request_count"] == 0


@pytest.mark.parametrize("target", ["../outside.json", ".env", "fixtures/synthetic.json", "output/live-runs/overwrite.json", "output/linked.json", "output/hardlinked.json"])
def test_artifact_scope_protects_keys_inputs_and_ledger(project, target):
    import os
    (project/"output").mkdir()
    (project/"output/linked.json").symlink_to(project/"fixtures/synthetic.json")
    os.link(project/"fixtures/synthetic.json",project/"output/hardlinked.json")
    with pytest.raises(GenerationBlocked): artifact_path(project,Path(target))


def test_strict_approval_flags_and_no_live_parameter_override(project):
    from pdf_notion_mvp.live_run_cli import main
    value=proposal(project)
    raw=value.model_dump(); raw["user_approved"]="true"
    with pytest.raises(ValueError): RunApproval.model_validate(raw)
    with pytest.raises(SystemExit) as exc:
        main(["--mode","live","--approval","output/approval.json","--expected-plan-sha256","0"*64,
            "--max-output-tokens","1024"],project_root=project,key_provider=lambda:pytest.fail("no key lookup"))
    assert exc.value.code == 1
