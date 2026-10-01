"""Extractive quiz generation: a local independent verifier, not semantic grading."""
import hashlib
import re
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import Field

from .contracts import Contract, FixtureInput
from .review import HierarchicalOutline, ReviewLayer, apply_review


class LessonEvidence(Contract):
    block_id: str
    text: str
    layer: Literal["authored_synthetic", "confirmed_transcription"]
    page: int


class LessonContext(Contract):
    document_id: str
    version: str
    source_digest: str
    section_id: str
    evidence: list[LessonEvidence]


class Question(Contract):
    # All fields are required and objects forbid extra fields for JSON Schema mode.
    question_id: str
    question: str
    answer: str
    explanation: str
    source_block_ids: list[str]
    source_quote: str


class QuizBatch(Contract):
    questions: list[Question]


class QuizPolicy(Contract):
    max_attempts: int = Field(default=2, ge=1, le=3)
    max_questions: int = Field(default=3, ge=1, le=3)
    model: str = ""
    allow_network: bool = False
    budget_confirmed: bool = False
    capabilities_confirmed: bool = False
    max_calls: int = Field(default=0, ge=0, le=3)
    max_request_bytes: int = Field(default=0, ge=0, le=100_000)
    max_output_tokens: int = Field(default=0, ge=0, le=4096)
    timeout_seconds: float = Field(default=20, gt=0, le=60, allow_inf_nan=False)


class QuizResult(Contract):
    status: Literal["ready_for_review", "failed_human_review"]
    attempts: int
    document_id: str
    version: str
    section_id: str
    source_digest: str
    evidence_digest: str
    batch: QuizBatch | None
    errors: list[str]
    events: list[str]
    human_review_required: Literal[True] = True
    semantic_correctness_verified: Literal[False] = False
    provider_mode: Literal["mock", "openai"]


class GenerationBlocked(RuntimeError):
    pass


class QuizGenerator(Protocol):
    mode: Literal["mock", "openai"]
    def generate(self, context: LessonContext, policy: QuizPolicy, feedback: list[str]) -> object: ...


def prepare_context(source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer,
                    section_id: str, asset_root: Path | None = None) -> LessonContext:
    # Do not trust a saved ReviewResult as an authority; recompute against inputs.
    reviewed = apply_review(source, hierarchy, layer, asset_root)
    section = next((s for s in reviewed.sections if s.section_id == section_id), None)
    if section is None:
        raise ValueError("unknown source section")
    by_id = {c.block_id: c for c in reviewed.layer.corrections}
    evidence = []
    for block in section.source_blocks:
        if block.kind != "text" or block.role != "body":
            continue
        correction = by_id.get(block.block_id)
        if correction and correction.status == "candidate":
            continue
        if source.kind == "ocr_ir" and (correction is None or correction.status != "confirmed"):
            continue
        evidence.append(LessonEvidence(block_id=block.block_id, text=section.effective_text[block.block_id],
            layer="authored_synthetic" if source.kind == "synthetic_ir" else "confirmed_transcription", page=block.source.page))
    if not evidence:
        raise ValueError("no confirmed text evidence in section")
    return LessonContext(document_id=reviewed.original.document_id, version=reviewed.original.version,
        source_digest=reviewed.source_digest, section_id=section_id, evidence=evidence)


def cloze_question(quote: str, answer: str) -> str:
    return "자료의 빈칸을 채우세요: " + quote.replace(answer, "[빈칸]", 1)


def verify_quiz(context: LessonContext, batch: QuizBatch, max_questions: int) -> list[str]:
    # Independent of the adapter and its self-reported confidence/citations.
    context = LessonContext.model_validate(context.model_dump())
    batch = QuizBatch.model_validate(batch.model_dump())
    errors = []
    if not 1 <= len(batch.questions) <= max_questions:
        errors.append("question_count")
    evidence = {e.block_id: e.text for e in context.evidence}
    ids, prompts, source_answers = set(), set(), set()
    for q in batch.questions:
        if not q.question_id.strip() or q.question_id in ids:
            errors.append("duplicate_or_empty_question_id")
        ids.add(q.question_id)
        if len(q.source_block_ids) != 1 or q.source_block_ids[0] not in evidence:
            errors.append("unsupported_source_reference")
            continue
        quote, answer = q.source_quote, q.answer
        if not quote or quote not in evidence[q.source_block_ids[0]]:
            errors.append("unsupported_quote")
        if not answer.strip() or answer == quote or answer not in quote or "[빈칸]" in quote:
            errors.append("unsupported_or_trivial_answer")
        if q.question != cloze_question(quote, answer):
            errors.append("unsupported_question_claim")
        if q.explanation != "근거 원문: " + quote:
            errors.append("unsupported_explanation_claim")
        normalized = re.sub(r"\s+", "", q.question).casefold()
        signature = (re.sub(r"\s+", "", quote).casefold(), re.sub(r"\s+", "", answer).casefold())
        if normalized in prompts or signature in source_answers:
            errors.append("duplicate_question")
        prompts.add(normalized)
        source_answers.add(signature)
    return sorted(set(errors))


class QuizState(TypedDict):
    context: LessonContext
    policy: QuizPolicy
    attempts: int
    batch: QuizBatch | None
    errors: list[str]
    events: list[str]
    terminal: bool


class QuizWorkflow:
    def __init__(self, generator: QuizGenerator):
        self.generator = generator
        graph = StateGraph(QuizState)
        graph.add_node("generate", self.generate)
        graph.add_node("independent_validate", self.validate)
        graph.add_edge(START, "generate")
        graph.add_edge("generate", "independent_validate")
        graph.add_conditional_edges("independent_validate", self.route, {"retry":"generate", "finish":END})
        self.graph = graph.compile()

    def generate(self, state: QuizState) -> QuizState:
        updated = dict(state, attempts=state["attempts"]+1, batch=None,
            events=state["events"]+["generate"])
        try:
            value = self.generator.generate(state["context"].model_copy(deep=True), state["policy"].model_copy(deep=True), list(state["errors"]))
            updated["batch"] = QuizBatch.model_validate_json(value) if isinstance(value, str) else QuizBatch.model_validate(value.model_dump() if isinstance(value, QuizBatch) else value)
            updated["errors"] = []
        except GenerationBlocked:
            updated["errors"] = ["generation_blocked"]
            updated["terminal"] = True
        except Exception as exc:
            # Provider error bodies can contain credentials or document content.
            updated["errors"] = ["generation_error:"+type(exc).__name__]
        return updated

    @staticmethod
    def validate(state: QuizState) -> QuizState:
        errors = state["errors"] if state["batch"] is None else verify_quiz(state["context"], state["batch"], state["policy"].max_questions)
        return dict(state, errors=errors, events=state["events"]+["independent_validate"])

    @staticmethod
    def route(state: QuizState) -> str:
        return "retry" if state["errors"] and not state["terminal"] and state["attempts"] < state["policy"].max_attempts else "finish"

    def run(self, source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer, section_id: str,
            policy: QuizPolicy | None = None, asset_root: Path | None = None) -> QuizResult:
        policy = QuizPolicy.model_validate((policy or QuizPolicy()).model_dump())
        context = prepare_context(source, hierarchy, layer, section_id, asset_root)
        initial = dict(context=context, policy=policy, attempts=0, batch=None, errors=[], events=[], terminal=False)
        state = self.graph.invoke(initial, config={"recursion_limit":12})
        result = QuizResult(status="failed_human_review" if state["errors"] else "ready_for_review",
            attempts=state["attempts"], document_id=context.document_id, version=context.version,
            section_id=section_id, source_digest=context.source_digest,
            evidence_digest=hashlib.sha256(context.model_dump_json().encode()).hexdigest(),
            batch=None if state["errors"] else state["batch"], errors=state["errors"], events=state["events"], provider_mode=self.generator.mode)
        return QuizResult.model_validate(result.model_dump())
