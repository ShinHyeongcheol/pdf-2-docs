"""Source-bound synthetic graph observations and closed mock inference candidates."""
import base64
import hashlib
import json
from copy import deepcopy
from xml.etree import ElementTree
from typing import Literal, Protocol

from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context
from pydantic import Field, model_validator

from .contracts import Box, Contract, FixtureInput, ImageBlock
from .notion_quiz import PageSnapshot, SectionPage, ToggleOperation, ToggleReceipt, digest, paragraph, toggle
from .review import HierarchicalOutline, ReviewLayer, apply_review

GRAPH_MARKER = "pdf-notion-graph:v1:"
INFERENCES = {
    "measurement_context_needed": "측정 조건의 차이가 있을 가능성은 추가 확인이 필요합니다.",
    "alternative_factor_possible": "관찰만으로 원인을 확정할 수 없으며 다른 요인 가능성은 추가 검토가 필요합니다.",
}
REVIEW_TEXT = {
    "semantic_unverified": "실제 시각 판독과 의미 진위는 검증하지 않았습니다.",
    "no_confirmed_observations": "확인된 관찰이 없어 수치·추세 설명을 확정하지 않습니다.",
    "unreadable_values": "판독 불가 영역의 수치·추세는 생성하지 않았습니다.",
    "extracted_candidates": "추출 후보는 사람이 확인하기 전까지 확정된 관찰로 사용하지 않습니다.",
}
ReviewReason = Literal["semantic_unverified", "no_confirmed_observations", "unreadable_values", "extracted_candidates"]


class GraphObservation(Contract):
    observation_id: str = Field(min_length=1, max_length=80)
    kind: Literal["label", "value", "trend"]
    status: Literal["confirmed", "extracted_candidate", "unreadable"]
    basis: Literal["authored_synthetic", "mock_extractor"]
    text: str | None = Field(default=None, max_length=1000)
    bbox: Box

    @model_validator(mode="after")
    def classification(self):
        if self.status == "unreadable":
            if self.text is not None:
                raise ValueError("unreadable observation must not claim a value or trend")
        elif not self.text or not self.text.strip():
            raise ValueError("readable observation requires evidence text")
        if (self.status == "confirmed") != (self.basis == "authored_synthetic"):
            raise ValueError("mock extraction cannot promote itself to confirmed")
        return self


class GraphEvidence(Contract):
    document_id: str
    version: str
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_id: str
    image_block_id: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations: list[GraphObservation] = Field(max_length=30)


class GraphContext(Contract):
    source_digest: str
    section_id: str
    image: ImageBlock
    image_media_type: Literal["image/svg+xml"] = "image/svg+xml"
    # Authored mock asset only; not sent to any remote model.
    image_base64: str
    evidence: GraphEvidence


def prepare_graph_context(source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer,
                          evidence: GraphEvidence, image_bytes: bytes) -> GraphContext:
    evidence = GraphEvidence.model_validate(evidence.model_dump())
    reviewed = apply_review(source, hierarchy, layer)
    if source.kind != "synthetic_ir" or len(image_bytes) > 100_000 or not image_bytes:
        raise ValueError("only a bounded authored synthetic graph asset is supported")
    # Validate the advertised mock media type; this is not pixel/semantic validation.
    if b"<!DOCTYPE" in image_bytes.upper() or b"<!ENTITY" in image_bytes.upper():
        raise ValueError("unsupported synthetic SVG declaration")
    try:
        asset = ElementTree.fromstring(image_bytes)
    except ElementTree.ParseError:
        raise ValueError("malformed authored SVG") from None
    if asset.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError("authored asset must be an SVG")
    if (evidence.document_id, evidence.version, evidence.source_digest) != (
        reviewed.original.document_id, reviewed.original.version, reviewed.source_digest):
        raise ValueError("graph evidence source mismatch")
    section = next((s for s in reviewed.sections if s.section_id == evidence.section_id), None)
    image = next((b for b in section.source_blocks if b.block_id == evidence.image_block_id), None) if section else None
    if not isinstance(image, ImageBlock):
        raise ValueError("graph must reference an image owned by the selected outline leaf")
    if image.sha256 != evidence.image_sha256 or hashlib.sha256(image_bytes).hexdigest() != image.sha256:
        raise ValueError("graph asset hash mismatch")
    seen = set()
    outer = image.source.bbox
    for observation in evidence.observations:
        if observation.observation_id in seen:
            raise ValueError("duplicate graph observation")
        seen.add(observation.observation_id)
        box = observation.bbox
        if not (outer.x0 <= box.x0 < box.x1 <= outer.x1 and outer.y0 <= box.y0 < box.y1 <= outer.y1):
            raise ValueError("observation bbox outside image source")
    return GraphContext(source_digest=reviewed.source_digest, section_id=evidence.section_id,
        image=image.model_copy(deep=True), image_base64=base64.b64encode(image_bytes).decode(), evidence=evidence)


def required_reviews(context: GraphContext) -> list[str]:
    observations = context.evidence.observations
    reviews = ["semantic_unverified"]
    if not any(o.status == "confirmed" for o in observations): reviews.append("no_confirmed_observations")
    if any(o.status == "unreadable" for o in observations): reviews.append("unreadable_values")
    if any(o.status == "extracted_candidate" for o in observations): reviews.append("extracted_candidates")
    return reviews


class GraphInference(Contract):
    kind: Literal["measurement_context_needed", "alternative_factor_possible"]
    basis_observation_ids: list[str] = Field(min_length=1, max_length=30)


class GraphDraft(Contract):
    image_block_id: str
    confirmed_observation_ids: list[str] = Field(max_length=30)
    extracted_observation_ids: list[str] = Field(max_length=30)
    inference_candidates: list[GraphInference] = Field(max_length=2)
    review_reasons: list[ReviewReason]


def verify_graph(context: GraphContext, draft: GraphDraft) -> list[str]:
    context = GraphContext.model_validate(context.model_dump())
    draft = GraphDraft.model_validate(draft.model_dump())
    errors = []
    if draft.image_block_id != context.image.block_id: errors.append("image_reference")
    by_id = {o.observation_id:o for o in context.evidence.observations}
    if draft.confirmed_observation_ids != [o.observation_id for o in by_id.values() if o.status == "confirmed"]:
        errors.append("confirmed_observation_coverage")
    if draft.extracted_observation_ids != [o.observation_id for o in by_id.values() if o.status == "extracted_candidate"]:
        errors.append("candidate_observation_coverage")
    if draft.review_reasons != required_reviews(context): errors.append("review_coverage")
    seen = set()
    for inference in draft.inference_candidates:
        if inference.kind in seen or len(set(inference.basis_observation_ids)) != len(inference.basis_observation_ids):
            errors.append("duplicate_inference")
        seen.add(inference.kind)
        if any(i not in by_id or by_id[i].status != "confirmed" or by_id[i].kind not in {"value", "trend"}
               for i in inference.basis_observation_ids):
            errors.append("unsupported_inference_basis")
    return sorted(set(errors))


class GraphResult(Contract):
    status: Literal["ready_for_review", "failed_human_review"]
    provider_mode: Literal["mock_multimodal"] = "mock_multimodal"
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_id: str
    image_block_id: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft: GraphDraft | None
    errors: list[str]
    events: list[str]
    human_review_required: Literal[True] = True
    actual_vision_verified: Literal[False] = False
    semantic_correctness_verified: Literal[False] = False


class GraphAdapter(Protocol):
    mode: Literal["mock_multimodal"]
    def generate(self, context: GraphContext) -> GraphDraft | dict | str: ...


class ScriptedGraphAdapter:
    mode = "mock_multimodal"

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.chain = RunnableLambda(self.respond)

    def respond(self, context):
        self.calls += 1
        if isinstance(self.response, Exception): raise self.response
        return deepcopy(self.response)

    def generate(self, context):
        return self.chain.invoke(context)


def generate_graph(source, hierarchy, layer, evidence, image_bytes, adapter: GraphAdapter) -> GraphResult:
    context = prepare_graph_context(source, hierarchy, layer, evidence, image_bytes)
    saved_context = context.model_dump_json()
    draft = None
    events = []
    try:
        if adapter.mode != "mock_multimodal": raise ValueError("actual graph model calls are disabled")
        events.append("mock_multimodal_generate")
        with tracing_context(enabled=False):
            value = adapter.generate(GraphContext.model_validate_json(saved_context))
        draft = GraphDraft.model_validate_json(value) if isinstance(value, str) else GraphDraft.model_validate(value.model_dump() if isinstance(value, GraphDraft) else value)
        events.append("independent_validate")
        errors = verify_graph(GraphContext.model_validate_json(saved_context), draft)
    except Exception as exc:
        errors = ["graph_error:"+type(exc).__name__]
    return GraphResult(status="failed_human_review" if errors else "ready_for_review",
        source_digest=context.source_digest, evidence_digest=digest(json.loads(saved_context)),
        section_id=context.section_id, image_block_id=context.image.block_id, image_sha256=context.image.sha256,
        draft=None if errors else draft, errors=errors, events=events)


class GraphTogglePlan(Contract):
    mode: Literal["mock_graph_review_only"] = "mock_graph_review_only"
    binding: SectionPage
    source_digest: str
    evidence_digest: str
    operations: list[ToggleOperation]
    human_review_required: Literal[True] = True
    actual_vision_verified: Literal[False] = False
    semantic_correctness_verified: Literal[False] = False


def plan_graph_toggle(source, hierarchy, layer, evidence, image_bytes, result, binding) -> GraphTogglePlan:
    result = GraphResult.model_validate(result.model_dump())
    binding = SectionPage.model_validate(binding.model_dump())
    context = prepare_graph_context(source, hierarchy, layer, evidence, image_bytes)
    if (result.status != "ready_for_review" or result.draft is None or result.errors or
        result.events != ["mock_multimodal_generate", "independent_validate"] or
        (result.source_digest, result.evidence_digest, result.section_id, result.image_block_id, result.image_sha256) !=
        (context.source_digest, digest(context.model_dump(mode="json")), context.section_id, context.image.block_id, context.image.sha256) or
        (binding.document_id, binding.version, binding.section_id) !=
        (context.image.source.document_id, context.image.source.version, context.section_id) or
        verify_graph(context, result.draft)):
        raise ValueError("graph result failed independent publication revalidation")
    by_id = {o.observation_id:o for o in context.evidence.observations}
    def observed(ids):
        return [paragraph(by_id[i].text+" | 근거: "+json.dumps(by_id[i].model_dump(mode="json"),ensure_ascii=False,sort_keys=True)) for i in ids]
    source_meta = {"image_block_id":context.image.block_id,"asset_ref":context.image.asset_ref,
        "image_sha256":context.image.sha256,"source":context.image.source.model_dump(mode="json"),
        "source_digest":context.source_digest,"evidence_digest":result.evidence_digest,
        "asset_bytes_hash_verified":True,"actual_vision_verified":False}
    key = digest(["graph-toggle-v1",context.model_dump(mode="json"),result.draft.model_dump(mode="json")])
    payload = toggle("[합성 그래프 설명 · 검토 필요] "+context.image.caption,[
        paragraph("모의 멀티모달 응답입니다. 확인된 관찰은 작성자가 정한 합성 근거이며 실제 vision 판독이 아닙니다."),
        paragraph("이미지 출처: "+json.dumps(source_meta,ensure_ascii=False,sort_keys=True)),
        toggle("추출 후보 · 미확정",observed(result.draft.extracted_observation_ids)),
        toggle("확인된 관찰 · 합성 근거",observed(result.draft.confirmed_observation_ids)),
        toggle("모의 모델 추론 후보 · 미확정",[paragraph(INFERENCES[i.kind]+" | 근거 ID: "+", ".join(i.basis_observation_ids)) for i in result.draft.inference_candidates]),
        toggle("검토 필요 사항",[paragraph(REVIEW_TEXT[r]) for r in result.draft.review_reasons]),
        toggle("게시 식별",[paragraph(GRAPH_MARKER+key)])])
    if len(json.dumps(payload,ensure_ascii=False).encode())>100_000: raise ValueError("graph plan exceeds local limit")
    return GraphTogglePlan(binding=binding,source_digest=context.source_digest,evidence_digest=result.evidence_digest,
        operations=[ToggleOperation(operation_key=key,block=payload)])


def _graph_marker_texts(value) -> list[str]:
    """Inspect every snapshot field; join rich text so split markers stay visible."""
    if isinstance(value, str):
        return [value] if GRAPH_MARKER in value else []
    if isinstance(value, list):
        return [text for item in value for text in _graph_marker_texts(item)]
    if not isinstance(value, dict):
        return []
    found = []
    for key, child in value.items():
        if key == "rich_text" and isinstance(child, list):
            text = "".join(item.get("text", {}).get("content", "") for item in child)
            found.extend(_graph_marker_texts(text))
            # Inspect remaining rich-text metadata without counting content twice.
            remainder = deepcopy(child)
            for item in remainder:
                if isinstance(item.get("text"), dict): item["text"].pop("content", None)
            found.extend(_graph_marker_texts(remainder))
        else:
            found.extend(_graph_marker_texts(child))
    return found


def graph_owned_key(block: dict) -> str | None:
    all_markers = _graph_marker_texts(block)
    if not all_markers:
        return None
    if block.get("type") != "toggle":
        raise ValueError("graph marker outside expected owner structure")
    markers = []
    for child in block.get("toggle", {}).get("children", []):
        if child.get("type") != "toggle":
            continue
        for item in child.get("toggle", {}).get("children", []):
            if item.get("type") != "paragraph":
                continue
            text = "".join(r.get("text", {}).get("content", "") for r in item.get("paragraph", {}).get("rich_text", []))
            if text.startswith(GRAPH_MARKER):
                markers.append(text[len(GRAPH_MARKER):])
    if not markers:
        raise ValueError("graph marker moved or nested outside expected owner structure")
    if len(markers) != 1 or len(markers[0]) != 64 or any(c not in "0123456789abcdef" for c in markers[0]):
        raise ValueError("malformed graph-owned marker")
    if all_markers != [GRAPH_MARKER+markers[0]]:
        raise ValueError("extra or detached graph marker")
    return markers[0]


def publish_graph_mock(source, hierarchy, layer, evidence, image_bytes, result, binding, gateway) -> ToggleReceipt:
    # Saved plans never authorize writes: reconstruct from source and evidence each time.
    plan = plan_graph_toggle(source, hierarchy, layer, evidence, image_bytes, result, binding)
    receipt = ToggleReceipt(status="unchanged")
    try:
        if gateway.mode != "mock":
            raise ValueError("live graph publishing is disabled")
        snapshot = PageSnapshot.model_validate(gateway.snapshot(plan.binding.model_copy(deep=True)).model_dump())
        if snapshot.binding != plan.binding or not snapshot.complete:
            raise ValueError("incomplete or mismatched graph page")
        existing = {}
        for block in snapshot.children:
            key = graph_owned_key(block)
            if key is not None:
                if key in existing:
                    raise ValueError("duplicate graph-owned markers")
                existing[key] = block
        for operation in plan.operations:
            if operation.operation_key in existing and existing[operation.operation_key] != operation.block:
                raise ValueError("graph-owned content edited; human review required")
        for operation in plan.operations:
            if operation.operation_key in existing:
                receipt.existing_keys.append(operation.operation_key)
            else:
                gateway.append(plan.binding.model_copy(deep=True), deepcopy(operation.block))
                receipt.appended_keys.append(operation.operation_key)
        receipt.status = "mock_written" if receipt.appended_keys else "unchanged"
    except Exception as exc:
        receipt.status = "failed_human_review"
        receipt.errors = ["graph_publish_error:"+type(exc).__name__]
    return receipt
