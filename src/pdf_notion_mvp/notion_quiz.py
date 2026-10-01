"""Native toggle plans and a mock-only append/reconcile path; no Notion transport."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from .contracts import Contract, FixtureInput
from .quiz import QuizResult, prepare_context, verify_quiz
from .review import HierarchicalOutline, ReviewLayer

MARKER = "pdf-notion-quiz:v1:"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


class SectionPage(Contract):
    hub_id: UUID
    page_id: UUID
    document_id: str
    version: str
    section_id: str


class ToggleOperation(Contract):
    operation_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    block: dict


class QuizTogglePlan(Contract):
    mode: Literal["mock_only", "provider_review_only"] = "mock_only"
    provider_mode: Literal["mock", "gemini", "openai"] = "mock"
    execution_mode: Literal["authored_mock", "injected", "network_unattested"] = "authored_mock"
    model: str = ""
    completion_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_id: UUID | None = None
    provider_attestation_verified: Literal[False] = False
    binding: SectionPage
    source_digest: str
    evidence_digest: str
    human_review_required: Literal[True] = True
    semantic_correctness_verified: Literal[False] = False
    operations: list[ToggleOperation]


class PageSnapshot(Contract):
    binding: SectionPage
    complete: bool
    # Gateway must exhaust pagination and hydrate app-owned nested toggles.
    children: list[dict]


class ToggleReceipt(Contract):
    status: Literal["mock_written", "unchanged", "failed_human_review"]
    appended_keys: list[str] = Field(default_factory=list)
    existing_keys: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    human_review_required: Literal[True] = True


class PageGateway(Protocol):
    mode: Literal["mock"]
    def snapshot(self, binding: SectionPage) -> PageSnapshot: ...
    def append(self, binding: SectionPage, block: dict) -> None: ...


def rich_text(text: str) -> list[dict]:
    if len(text) > 20_000:
        raise ValueError("text exceeds local plan limit")
    return [{"type": "text", "text": {"content": text[i:i+2000]}}
            for i in range(0, len(text), 2000)]


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(text)}}


def toggle(text: str, children: list[dict]) -> dict:
    return {"object": "block", "type": "toggle", "toggle": {"rich_text": rich_text(text), "children": children}}


def plan_toggles(source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer,
                 result: QuizResult, binding: SectionPage, asset_root: Path | None = None) -> QuizTogglePlan:
    result = QuizResult.model_validate(result.model_dump())
    binding = SectionPage.model_validate(binding.model_dump())
    if result.provider_mode != "mock" or result.status != "ready_for_review" or result.batch is None or result.errors:
        raise ValueError("only validated authored mock results are supported")
    context = prepare_context(source, hierarchy, layer, result.section_id, asset_root)
    if (binding.document_id, binding.version, binding.section_id) != (context.document_id, context.version, context.section_id):
        raise ValueError("section page binding mismatch")
    if (result.document_id, result.version, result.source_digest, result.evidence_digest) != (
            context.document_id, context.version, context.source_digest,
            hashlib.sha256(context.model_dump_json().encode()).hexdigest()):
        raise ValueError("stale or mismatched quiz provenance")
    if verify_quiz(context, result.batch, 3):
        raise ValueError("quiz failed independent revalidation")
    return _render_verified_plan(source, result, binding, context)


def _render_verified_plan(source, result, binding, context, *, provider="mock", model="",
                          execution_mode="authored_mock", completion_sha256=None, run_id=None):
    """Internal renderer: callers must independently revalidate their input boundary."""
    by_id = {b.block_id: b for b in source.document.blocks}
    evidence = {e.block_id: e for e in context.evidence}
    operations = []
    for q in result.batch.questions:
        # Provider-assigned IDs/attempt counts do not alter publication identity.
        content = q.model_dump(exclude={"question_id"})
        identity = "mock" if provider == "mock" else ["provider-review-v1", provider, model, execution_mode]
        key = digest(["quiz-toggle-v1", context.model_dump(), identity, content])
        block_id = q.source_block_ids[0]
        block = by_id[block_id]
        provenance_data = {"document_id": context.document_id, "version": context.version,
            "section_id": context.section_id, "source_digest": context.source_digest,
            "evidence_digest": result.evidence_digest, "block_id": block_id,
            "page": block.source.page, "bbox": block.source.bbox.model_dump(),
            "evidence_layer": evidence[block_id].layer}
        if provider == "mock":
            title = "[모의 문제 · 검토 필요] "
            disclosure = "직접 작성한 모의 결과입니다. 실제 모델 생성·의미 진위 검증을 하지 않았습니다."
        else:
            provenance_data.update(provider=provider, model=model, execution_mode=execution_mode,
                provider_attestation_verified=False)
            title = "[주입 실행 · 검토 필요] " if execution_mode == "injected" else "[제공자 결과 · 검토 필요] "
            disclosure = (f"{provider}/{model} 어댑터의 검토 후보입니다. 원문 인용·빈칸 형식·출처를 재검증했습니다. "
                "실제 외부 호출 여부·제공자 인증·의미 진위·청구액은 독립 검증하지 않았습니다.")
        provenance = json.dumps(provenance_data, ensure_ascii=False, sort_keys=True)
        payload = toggle(title + q.question, [paragraph(disclosure),
            toggle("정답", [paragraph(q.answer)]),
            toggle("해설과 원문 출처", [paragraph(q.explanation), paragraph("출처: " + provenance),
                paragraph("원본 전사: " + block.text),
                paragraph("학습 근거: " + evidence[block_id].text), paragraph(MARKER + key)])])
        operations.append(ToggleOperation(operation_key=key, block=payload))
    if len(json.dumps([o.block for o in operations], ensure_ascii=False).encode()) > 100_000:
        raise ValueError("toggle payload exceeds local plan byte limit")
    return QuizTogglePlan(mode="mock_only" if provider == "mock" else "provider_review_only",
        provider_mode=provider, execution_mode=execution_mode, model=model,
        completion_sha256=completion_sha256, run_id=run_id, binding=binding,
        source_digest=context.source_digest, evidence_digest=result.evidence_digest, operations=operations)


def owned_key(block: dict) -> str | None:
    if block.get("type") != "toggle":
        return None
    markers = []
    for child in block.get("toggle", {}).get("children", []):
        if child.get("type") != "toggle":
            continue
        for p in child.get("toggle", {}).get("children", []):
            if p.get("type") == "paragraph":
                text = "".join(r.get("text", {}).get("content", "") for r in p.get("paragraph", {}).get("rich_text", []))
                if text.startswith(MARKER):
                    markers.append(text[len(MARKER):])
    if not markers:
        return None
    if len(markers) != 1 or len(markers[0]) != 64 or any(c not in "0123456789abcdef" for c in markers[0]):
        raise ValueError("malformed app-owned marker")
    return markers[0]


def _reconcile(plan: QuizTogglePlan, gateway: PageGateway) -> ToggleReceipt:
    """Internal execution boundary: call publish_mock to revalidate authoritative inputs."""
    receipt = ToggleReceipt(status="unchanged")
    try:
        if gateway.mode != "mock":
            raise ValueError("live Notion publishing is disabled")
        snapshot = PageSnapshot.model_validate(gateway.snapshot(plan.binding.model_copy(deep=True)).model_dump())
        if snapshot.binding != plan.binding or not snapshot.complete:
            raise ValueError("incomplete or mismatched page snapshot")
        existing = {}
        for block in snapshot.children:
            key = owned_key(block)
            if key is not None:
                if key in existing:
                    raise ValueError("duplicate remote app-owned markers")
                existing[key] = block
        # Preflight every operation before any append; preserve conflicting user edits.
        for operation in plan.operations:
            if operation.operation_key in existing and existing[operation.operation_key] != operation.block:
                raise ValueError("app-owned content edited; human review required")
        for operation in plan.operations:
            if operation.operation_key in existing:
                receipt.existing_keys.append(operation.operation_key)
            else:
                gateway.append(plan.binding.model_copy(deep=True), deepcopy(operation.block))
                receipt.appended_keys.append(operation.operation_key)
        receipt.status = "mock_written" if receipt.appended_keys else "unchanged"
    except Exception as exc:
        # No blind retry after an ambiguous write; next run reads the remote marker.
        receipt.status = "failed_human_review"
        receipt.errors = ["publish_error:" + type(exc).__name__]
    return receipt


def publish_mock(source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer,
                 result: QuizResult, binding: SectionPage, gateway: PageGateway,
                 asset_root: Path | None = None) -> ToggleReceipt:
    # Saved 'ready' results and modified plans are never authorities for publishing.
    plan = plan_toggles(source, hierarchy, layer, result, binding, asset_root)
    return _reconcile(plan, gateway)


class MemoryPageGateway:
    """A fake remote page surviving publisher restarts, with append-only writes."""
    mode = "mock"

    def __init__(self, binding: SectionPage, children: list[dict] | None = None):
        self.binding = binding.model_copy(deep=True)
        self.children = deepcopy(children or [])
        self.append_calls = 0

    def snapshot(self, binding: SectionPage) -> PageSnapshot:
        if binding != self.binding:
            raise ValueError("unknown page")
        return PageSnapshot(binding=self.binding.model_copy(deep=True), complete=True, children=deepcopy(self.children))

    def append(self, binding: SectionPage, block: dict) -> None:
        if binding != self.binding:
            raise ValueError("unknown page")
        self.children.append(deepcopy(block))
        self.append_calls += 1
