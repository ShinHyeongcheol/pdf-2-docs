"""Single approved synthetic run; no keys or transport until all local gates pass."""
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from .contracts import Contract, FixtureInput
from .openai_adapter import request_messages
from .providers import ProviderSettings, build_provider
from .quiz import GenerationBlocked, QuizBatch, QuizPolicy, QuizWorkflow, prepare_context
from .review import HierarchicalOutline, ReviewLayer

PRICE_URL = "https://ai.google.dev/gemini-api/docs/pricing"
MODEL_URL = "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite"
FILES = ("synthetic.json", "synthetic-outline.json", "synthetic-review.json", "synthetic-quiz.json")


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ModelSnapshot(Contract):
    provider: Literal["gemini"] = "gemini"
    model: Literal["gemini-3.1-flash-lite"] = "gemini-3.1-flash-lite"
    tier: Literal["standard_paid_text"] = "standard_paid_text"
    input_usd_per_million: Decimal = Decimal("0.25")
    output_usd_per_million: Decimal = Decimal("1.50")
    input_token_capacity: int = 1_048_576
    output_token_capacity: int = 65_536
    checked_on: date = date(2026, 10, 1)
    pricing_url: str = PRICE_URL
    limits_url: str = MODEL_URL


class RunSpec(Contract):
    schema_version: Literal["1"] = "1"
    run_id: UUID
    created_at: datetime
    expires_at: datetime
    model_snapshot: ModelSnapshot
    input_kind: Literal["authored_synthetic_fixture"] = "authored_synthetic_fixture"
    input_digests: dict[str, str]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_id: Literal["unit.part"] = "unit.part"
    max_requests: Literal[1] = 1
    max_attempts: Literal[1] = 1
    max_questions: Literal[1] = 1
    max_request_bytes: int = Field(default=12_000, ge=1, le=12_000, strict=True)
    max_output_tokens: int = Field(default=512, ge=1, le=512, strict=True)
    timeout_seconds: float = Field(default=20, gt=0, le=20, allow_inf_nan=False)
    conservative_input_tokens: int = 1_048_576
    conservative_billable_output_tokens: int = 65_536
    budget_usd: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    cost_basis: Literal["full_model_token_capacities_at_reviewed_standard_prices"] = "full_model_token_capacities_at_reviewed_standard_prices"


class RunApproval(Contract):
    spec: RunSpec
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_approved: bool = Field(default=False, strict=True)
    data_transfer_confirmed: bool = Field(default=False, strict=True)
    pricing_and_limits_confirmed: bool = Field(default=False, strict=True)
    model_capabilities_confirmed: bool = Field(default=False, strict=True)
    conditional_cost_understood: bool = Field(default=False, strict=True)


def cost_envelope(spec: RunSpec) -> Decimal:
    return (spec.model_snapshot.input_usd_per_million * spec.conservative_input_tokens +
            spec.model_snapshot.output_usd_per_million * spec.conservative_billable_output_tokens) / Decimal(1_000_000)


def policy_for(spec: RunSpec) -> QuizPolicy:
    return QuizPolicy(model=spec.model_snapshot.model, allow_network=True, budget_confirmed=True,
        capabilities_confirmed=True, max_calls=1, max_attempts=1, max_questions=1,
        max_request_bytes=spec.max_request_bytes, max_output_tokens=spec.max_output_tokens,
        timeout_seconds=spec.timeout_seconds)


def load_inputs(project_root: Path):
    paths = [project_root / "fixtures" / name for name in FILES]
    if any(path.is_symlink() or path.stat().st_size > 100_000 for path in paths):
        raise GenerationBlocked("unsupported fixture path or size")
    raw = [path.read_bytes() for path in paths]
    source = FixtureInput.model_validate_json(raw[0])
    if source.kind != "synthetic_ir":
        raise GenerationBlocked("only authored synthetic fixtures are supported")
    hierarchy = HierarchicalOutline.model_validate_json(raw[1])
    layer = ReviewLayer.model_validate_json(raw[2])
    batch = QuizBatch.model_validate_json(raw[3])
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in zip(FILES, raw)}
    return source, hierarchy, layer, batch, digests


def expected_request(context, policy: QuizPolicy) -> dict:
    messages = request_messages(context, policy, [])
    return {"contents": [{"parts": [{"text": messages[1].content}], "role": "user"}],
        "systemInstruction": {"parts": [{"text": messages[0].content}]}, "safetySettings": [],
        "generationConfig": {"candidateCount": 1,
            "maxOutputTokens": policy.max_output_tokens, "responseMimeType": "application/json",
            "responseJsonSchema": QuizBatch.model_json_schema()}}


def create_proposal(project_root: Path, *, model: str, budget_usd: Decimal,
                    max_request_bytes=12_000, max_output_tokens=512, timeout_seconds=20,
                    now: datetime | None = None) -> RunApproval:
    now = now or datetime.now(timezone.utc)
    if model != ModelSnapshot().model:
        raise GenerationBlocked("this first live entry point supports only the reviewed Gemini model")
    source, hierarchy, layer, _, digests = load_inputs(project_root)
    context = prepare_context(source, hierarchy, layer, "unit.part")
    spec = RunSpec(run_id=uuid4(), created_at=now, expires_at=now+timedelta(hours=1),
        model_snapshot=ModelSnapshot(), input_digests=digests, source_digest=context.source_digest,
        context_digest=fingerprint(context.model_dump(mode="json")), request_contract_digest="0"*64,
        budget_usd=budget_usd, max_request_bytes=max_request_bytes,
        max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)
    spec.request_contract_digest = fingerprint(expected_request(context, policy_for(spec)))
    approval = RunApproval(spec=spec, plan_sha256=fingerprint(spec.model_dump(mode="json")))
    validate_plan(approval, project_root, now=now, require_approval=False)
    return approval


def validate_plan(approval: RunApproval, project_root: Path, *, now=None, require_approval=True):
    approval = RunApproval.model_validate(approval.model_dump())
    spec = approval.spec
    now = now or datetime.now(timezone.utc)
    if fingerprint(spec.model_dump(mode="json")) != approval.plan_sha256:
        raise GenerationBlocked("plan fingerprint mismatch")
    if any(d.tzinfo is None or d.utcoffset() is None for d in (spec.created_at, spec.expires_at, now)):
        raise GenerationBlocked("timezone-aware approval times required")
    if not spec.created_at <= now < spec.expires_at or not timedelta(0) < spec.expires_at-spec.created_at <= timedelta(hours=1):
        raise GenerationBlocked("expired or invalid approval window")
    if spec.model_snapshot != ModelSnapshot() or not 0 <= (now.date()-spec.model_snapshot.checked_on).days <= 7:
        raise GenerationBlocked("price/capability snapshot must be reviewed again")
    if (spec.conservative_input_tokens != spec.model_snapshot.input_token_capacity or
        spec.conservative_billable_output_tokens != spec.model_snapshot.output_token_capacity or
        spec.max_output_tokens > spec.conservative_billable_output_tokens):
        raise GenerationBlocked("unsupported smaller token cost bound")
    if cost_envelope(spec) > spec.budget_usd:
        raise GenerationBlocked("reviewed conditional cost envelope exceeds budget")
    if require_approval and not all((approval.user_approved, approval.data_transfer_confirmed,
        approval.pricing_and_limits_confirmed, approval.model_capabilities_confirmed,
        approval.conditional_cost_understood)):
        raise GenerationBlocked("explicit per-run approvals required")
    source, hierarchy, layer, batch, digests = load_inputs(project_root)
    context = prepare_context(source, hierarchy, layer, spec.section_id)
    expected = expected_request(context, policy_for(spec))
    if (digests != spec.input_digests or context.source_digest != spec.source_digest or
        fingerprint(context.model_dump(mode="json")) != spec.context_digest or
        fingerprint(expected) != spec.request_contract_digest):
        raise GenerationBlocked("approved inputs or request contract changed")
    if len(json.dumps(expected, ensure_ascii=False).encode()) > spec.max_request_bytes:
        raise GenerationBlocked("prepared request exceeds byte budget")
    return source, hierarchy, layer, batch


def artifact_path(project_root: Path, path: Path) -> Path:
    """Only ignored JSON outputs; never follow a symlink/hardlink to inputs or keys."""
    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    if candidate.suffix != ".json" or candidate.is_symlink():
        raise GenerationBlocked("local JSON output required")
    if any(parent.is_symlink() for parent in candidate.parents if parent != root):
        raise GenerationBlocked("artifact symlinks are unsupported")
    output = root / "output"
    resolved = candidate.resolve()
    if not resolved.is_relative_to(output) or resolved.is_relative_to(output / "live-runs"):
        raise GenerationBlocked("artifact must be under output outside the run ledger")
    if candidate.exists() and (not candidate.is_file() or candidate.stat().st_nlink != 1 or candidate.stat().st_size > 100_000):
        raise GenerationBlocked("unsupported artifact file")
    return candidate


def reserve_run(project_root: Path, approval: RunApproval) -> Path:
    ledger = project_root.resolve() / "output" / "live-runs"
    if any(path.is_symlink() for path in (ledger, ledger.parent)):
        raise GenerationBlocked("run ledger symlinks are unsupported")
    ledger.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ledger / (str(approval.spec.run_id)+".json")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise GenerationBlocked("run already reserved; do not replay uncertain calls") from None
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"run_id": str(approval.spec.run_id), "plan_sha256": approval.plan_sha256,
            "state": "consumed_before_key_lookup", "max_requests": 1}, stream)
    return path


def execute_approved(approval: RunApproval, project_root: Path, *, expected_plan_sha256: str,
                     client_factory=None, key_provider=None, now=None) -> dict:
    approval = RunApproval.model_validate(approval.model_dump())
    if approval.plan_sha256 != expected_plan_sha256:
        raise GenerationBlocked("explicit plan fingerprint required")
    source, hierarchy, layer, _ = validate_plan(approval, project_root, now=now)
    reserve_run(project_root, approval)  # Failure/timeout never refunds this run ID.
    requests = 0
    def validate_request(request: httpx.Request):
        nonlocal requests
        spec = approval.spec
        if requests or fingerprint(json.loads(request.content)) != spec.request_contract_digest:
            raise GenerationBlocked("finalized request differs from the approved contract")
        if len(request.content) > spec.max_request_bytes:
            raise GenerationBlocked("finalized byte budget exceeded")
        timeouts = request.extensions.get("timeout", {})
        if set(timeouts) != {"connect", "read", "write", "pool"} or any(value != spec.timeout_seconds for value in timeouts.values()):
            raise GenerationBlocked("finalized timeout differs from approval")
        requests += 1
    model = approval.spec.model_snapshot.model
    adapter = build_provider(ProviderSettings(provider="gemini", model=model, approved_models=[model]),
        project_root=project_root, client_factory=client_factory, key_provider=key_provider,
        request_validator=validate_request)
    result = QuizWorkflow(adapter).run(source, hierarchy, layer, approval.spec.section_id, policy_for(approval.spec))
    if result.status == "ready_for_review" and requests != 1:
        result.status = "failed_human_review"
        result.batch = None
        result.errors = ["approved_request_not_observed"]
    return {"run_id": str(approval.spec.run_id), "plan_sha256": approval.plan_sha256,
        "status": result.status, "provider_mode": result.provider_mode,
        "request_count": requests, "run_consumed": True,
        "conditional_cost_envelope_usd": str(cost_envelope(approval.spec)),
        "provider_billed_cost_verified": False, "provider_usage_verified": False,
        "result": result.model_dump(mode="json")}
