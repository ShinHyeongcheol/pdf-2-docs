import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from langchain_core.runnables import RunnableLambda

from pdf_notion_mvp.gemini_adapter import GeminiQuizAdapter
from pdf_notion_mvp.providers import ProviderSettings, build_provider, selected_key
from pdf_notion_mvp.quiz import GenerationBlocked, QuizBatch, QuizPolicy, QuizWorkflow
from pdf_notion_mvp.review import HierarchicalOutline, ReviewLayer


@pytest.fixture
def inputs(source):
    root = Path(__file__).parents[1] / "fixtures"
    hierarchy = HierarchicalOutline.model_validate_json((root / "synthetic-outline.json").read_text())
    layer = ReviewLayer.model_validate_json((root / "synthetic-review.json").read_text())
    batch = QuizBatch.model_validate_json((root / "synthetic-quiz.json").read_text())
    return source, hierarchy, layer, batch


def policy(**kwargs):
    return QuizPolicy(allow_network=True, budget_confirmed=True, capabilities_confirmed=True,
        max_calls=kwargs.pop("max_calls", 1), max_request_bytes=kwargs.pop("max_request_bytes", 20_000),
        max_output_tokens=200, **kwargs)


def run(adapter, inputs, p=None):
    return QuizWorkflow(adapter).run(*inputs[:3], "unit.part", p)


def settings(provider="gemini", model="gemini-fabricated-text"):
    return ProviderSettings(provider=provider, model=model, approved_models=[model])


@pytest.mark.parametrize("provider,model", [("gemini", "gemini-fabricated-text"), ("openai", "fabricated-text-model")])
def test_provider_switch_same_verifier_no_fallback(inputs, provider, model):
    calls = []
    class Client:
        def __init__(self, **kwargs): calls.append(kwargs)
        def with_structured_output(self, schema, **kwargs):
            assert schema is QuizBatch and kwargs["method"] == "json_schema"
            return RunnableLambda(lambda _: inputs[3])
    adapter = build_provider(settings(provider, model), client_factory=Client, key_provider=lambda: "fabricated-key")
    result = run(adapter, inputs, policy())
    assert result.status == "ready_for_review" and result.provider_mode == provider
    assert calls[0]["model"] == model and calls[0]["max_retries"] == 0
    if provider == "gemini":
        assert calls[0]["vertexai"] is False and calls[0]["timeout"] == 20_000
        assert calls[0]["client_args"]["trust_env"] is False
    else: assert calls[0]["base_url"] == "https://api.openai.com/v1"


@pytest.mark.parametrize("gate", ["default", "network", "budget", "capabilities", "calls", "bytes", "output", "model", "unapproved", "mismatch", "model_path", "provider_model"])
def test_gemini_gates_before_any_key_or_file_read(inputs, gate):
    s = settings()
    p = policy()
    if gate == "default": p = QuizPolicy()
    elif gate in ("network", "budget", "capabilities"): setattr(p, {"network":"allow_network", "budget":"budget_confirmed", "capabilities":"capabilities_confirmed"}[gate], False)
    elif gate == "calls": p.max_calls = 0
    elif gate == "bytes": p.max_request_bytes = 1
    elif gate == "output": p.max_output_tokens = 0
    elif gate == "model": s.model = ""
    elif gate == "unapproved": s.approved_models = []
    elif gate == "mismatch": p.model = "different"
    elif gate == "model_path": s.model = "gemini-path/escape"; s.approved_models = [s.model]
    elif gate == "provider_model": s.model = "unsupported-model"; s.approved_models = [s.model]
    reads = []
    adapter = build_provider(s, key_provider=lambda: reads.append("read") or "fabricated", client_factory=lambda **kw: reads.append("client"))
    result = run(adapter, inputs, p)
    assert result.errors == ["generation_blocked"] and reads == []


def test_unknown_provider_error_is_generic_and_does_not_echo_input():
    with pytest.raises(GenerationBlocked) as exc:
        ProviderSettings.from_env({"PDF_NOTION_PROVIDER": "secret-unsupported-provider"})
    assert "secret" not in str(exc.value)
    assert ProviderSettings.from_env({}).provider == "gemini"


def test_missing_key_does_not_construct_sdk(inputs):
    calls = []
    result = run(build_provider(settings(), key_provider=lambda: None, client_factory=lambda **kw: calls.append(kw)), inputs, policy())
    assert result.errors == ["generation_blocked"] and calls == []


@pytest.mark.parametrize("failure", [TimeoutError("fabricated-key"), ValueError("fabricated-key"), "{", {"questions":[]}])
def test_failures_have_bounded_budget_and_no_secret_logs(inputs, failure, caplog):
    requests = []
    class Client:
        def __init__(self, **kwargs): requests.append(kwargs)
        def with_structured_output(self, *args, **kwargs):
            def respond(_):
                if isinstance(failure, Exception): raise failure
                return failure
            return RunnableLambda(respond)
    adapter = build_provider(settings(), key_provider=lambda:"fabricated-key", client_factory=Client)
    result = run(adapter, inputs, policy())
    assert result.status == "failed_human_review" and result.attempts == 2
    assert len(requests) == 1 and "fabricated-key" not in result.model_dump_json() + caplog.text


def test_shared_gemini_budget_reservation_atomic(inputs):
    barrier = Barrier(2)
    calls = []
    def key(): barrier.wait(timeout=3); return "fabricated-key"
    class Client:
        def __init__(self, **kwargs): calls.append(1)
        def with_structured_output(self, *args, **kwargs): return RunnableLambda(lambda _:inputs[3])
    adapter = build_provider(settings(), key_provider=key, client_factory=Client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:run(adapter, inputs, policy()), range(2)))
    assert len(calls) == 1 and sorted(r.status for r in results) == ["failed_human_review", "ready_for_review"]


def test_selected_dotenv_key_only_no_shell_or_environment_mutation(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / ".env").write_text('GEMINI_API_KEY="fabricated-gemini"\nOPENAI_API_KEY=fabricated-openai\nUNRELATED=$(touch forbidden)\n')
    assert selected_key("GEMINI_API_KEY", tmp_path) == "fabricated-gemini"
    assert os.environ == {} and not (tmp_path / "forbidden").exists()
    os.environ["GEMINI_API_KEY"] = "fabricated-process-key"
    assert selected_key("GEMINI_API_KEY", tmp_path) == "fabricated-process-key"


@pytest.mark.parametrize("content", ['GEMINI_API_KEY=\n', 'GEMINI_API_KEY=$SECRET\n', 'GEMINI_API_KEY="unterminated\n', 'GEMINI_API_KEY=one\nGEMINI_API_KEY=two\n'])
def test_selected_dotenv_errors_are_generic(tmp_path, monkeypatch, content):
    import os
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / ".env").write_text(content)
    with pytest.raises(GenerationBlocked): selected_key("GEMINI_API_KEY", tmp_path)


def test_unapproved_run_never_opens_existing_dotenv(inputs, tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / ".env").write_text("GEMINI_API_KEY=fabricated-only\n")
    def forbidden(*args, **kwargs): raise AssertionError("must not read secret file")
    monkeypatch.setattr(Path, "read_text", forbidden)
    result = run(build_provider(settings(), project_root=tmp_path), inputs)
    assert result.errors == ["generation_blocked"]


@pytest.mark.parametrize("violation", [None, "port", "host", "path", "bytes", "error", "refusal", "invalid_json"])
def test_actual_gemini_sdk_with_mock_transport_only(inputs, monkeypatch, violation, caplog):
    import os
    from langchain_google_genai import ChatGoogleGenerativeAI
    monkeypatch.setattr(os, "environ", {})  # Never read inherited keys/configuration.
    seen = []
    def respond(request):
        seen.append(request)
        if violation == "error": return httpx.Response(503, json={"error":{"code":503,"message":"fabricated-key","status":"UNAVAILABLE"}})
        if violation == "refusal": return httpx.Response(200, json={"promptFeedback":{"blockReason":"SAFETY"}})
        payload = "{" if violation == "invalid_json" else inputs[3].model_dump_json()
        return httpx.Response(200, json={"candidates":[{"content":{"role":"model","parts":[{"text":payload}]},"finishReason":"STOP"}]})
    def factory(**kwargs):
        args = dict(kwargs["client_args"])
        args["transport"] = httpx.MockTransport(respond)
        kwargs["client_args"] = args
        if violation == "port": kwargs["base_url"] = "https://generativelanguage.googleapis.com:444"
        if violation == "host": kwargs["base_url"] = "https://other.example"
        if violation == "path": kwargs["api_version"] = "v2"
        model = ChatGoogleGenerativeAI(**kwargs)
        if violation == "bytes":
            from types import SimpleNamespace
            from langchain_core.messages import HumanMessage
            def structured(*args, **kw):
                return RunnableLambda(lambda messages: messages + [HumanMessage("x" * 10_000)]) | model.with_structured_output(*args, **kw)
            return SimpleNamespace(client=model.client, with_structured_output=structured)
        return model
    p = policy(max_request_bytes=10_000)
    if violation == "bytes": p.max_request_bytes = 3000
    result = run(build_provider(settings(), client_factory=factory, key_provider=lambda:"fabricated-key"), inputs, p)
    assert "fabricated-key" not in result.model_dump_json() + caplog.text
    if violation is None:
        assert result.status == "ready_for_review" and len(seen) == 1
        body = json.loads(seen[0].content)
        config = body["generationConfig"]
        assert config["responseMimeType"] == "application/json" and config["maxOutputTokens"] == 200
        assert "responseJsonSchema" in config
        assert seen[0].url.path == "/v1beta/models/gemini-fabricated-text:generateContent"
    elif violation in ("port", "host", "path", "bytes"):
        assert result.status == "failed_human_review" and seen == []
    else:
        assert result.status == "failed_human_review" and len(seen) == 1


def test_gemini_rejects_unsupported_claim_with_shared_independent_verifier(inputs):
    batch = inputs[3].model_copy(deep=True)
    batch.questions[0].source_block_ids = ["unknown"]
    class Client:
        def __init__(self, **kwargs): pass
        def with_structured_output(self, *args, **kwargs): return RunnableLambda(lambda _:batch)
    result = run(build_provider(settings(), client_factory=Client, key_provider=lambda:"fabricated-key"), inputs, policy(max_calls=2))
    assert result.status == "failed_human_review" and "unsupported_source_reference" in result.errors
    assert result.attempts == 2


def test_approved_dotenv_execution_fake_client_only(inputs, tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / ".env").write_text("GEMINI_API_KEY=fabricated-file-key\n")
    class Client:
        def __init__(self, **kwargs): assert kwargs["api_key"] == "fabricated-file-key"
        def with_structured_output(self, *args, **kwargs): return RunnableLambda(lambda _:inputs[3])
    result = run(build_provider(settings(), project_root=tmp_path, client_factory=Client), inputs, policy())
    assert result.status == "ready_for_review" and os.environ == {}


def test_configuration_cli_never_reads_keys_or_files(monkeypatch, capsys):
    import os
    import sys
    from pdf_notion_mvp.provider_cli import main
    class NonSecretEnv(dict):
        def get(self, name, default=None):
            assert name in {"PDF_NOTION_PROVIDER", "PDF_NOTION_MODEL", "PDF_NOTION_APPROVED_MODELS", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG", "COLUMNS", "LINES"}
            return super().get(name, default)
    monkeypatch.setattr(os, "environ", NonSecretEnv())
    monkeypatch.setattr(sys, "argv", ["provider-check", "--provider", "gemini"])
    main()
    output = capsys.readouterr().out
    assert "provider=gemini" in output and "key_lookup=disabled" in output


def test_dotenv_symlink_and_size_rejected(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", {})
    target = tmp_path / "other"
    target.write_text("GEMINI_API_KEY=fabricated")
    (tmp_path / ".env").symlink_to(target)
    with pytest.raises(GenerationBlocked): selected_key("GEMINI_API_KEY", tmp_path)
    (tmp_path / ".env").unlink()
    (tmp_path / ".env").write_text("x" * 20_000)
    with pytest.raises(GenerationBlocked): selected_key("GEMINI_API_KEY", tmp_path)


def test_quiz_disables_inherited_tracing(inputs, monkeypatch):
    import os
    from langsmith.utils import tracing_is_enabled
    monkeypatch.setattr(os, "environ", {"LANGSMITH_TRACING":"true"})
    observations = []
    class Client:
        def __init__(self, **kwargs): observations.append(tracing_is_enabled())
        def with_structured_output(self, *args, **kwargs): return RunnableLambda(lambda _:inputs[3])
    result = run(build_provider(settings(), client_factory=Client, key_provider=lambda:"fabricated-key"), inputs, policy())
    assert result.status == "ready_for_review" and observations == [False]
