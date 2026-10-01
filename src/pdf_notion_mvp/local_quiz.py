"""Deterministic source cloze rules and a neutral review plan; no model adapter."""
import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract, Source
from .notion_quiz import digest
from .quiz import Question, QuizBatch, cloze_question, prepare_context, verify_quiz

RULE_VERSION = "source-cloze-first-token-v1"
MAX_QUOTE_CHARS = 2000


class ReviewedRuleQuestion(Contract):
    operation_key: str
    question: Question
    source: Source
    evidence_layer: Literal["authored_synthetic", "confirmed_transcription"]
    original_text: str
    effective_text: str
    correction_id: str | None


class LocalQuizPlan(Contract):
    mode: Literal["local_rule_review_only"] = "local_rule_review_only"
    generation_mode: Literal["deterministic_cloze_rule"] = "deterministic_cloze_rule"
    rule_version: Literal["source-cloze-first-token-v1"] = RULE_VERSION
    provider: None = None
    target: None = None
    status: Literal["ready_for_review", "failed_human_review"]
    document_id: str
    version: str
    section_id: str
    source_digest: str
    evidence_digest: str
    review_digest: str
    source_pages: int
    source_blocks: int
    source_ir_unmodified: Literal[True] = True
    questions: list[ReviewedRuleQuestion] = Field(max_length=3)
    errors: list[str]
    events: list[str]
    human_review_required: Literal[True] = True
    semantic_correctness_verified: Literal[False] = False
    actual_model_generation: Literal[False] = False


def rule_batch(context, max_questions: int) -> QuizBatch:
    context = type(context).model_validate(context.model_dump())
    questions, seen = [], set()
    for evidence in context.evidence:
        quote = evidence.text
        if len(quote)>MAX_QUOTE_CHARS or "[빈칸]" in quote:
            continue
        words = re.findall(r"[A-Za-z가-힣][A-Za-z0-9가-힣_]*", quote)
        if len(words)<2:
            continue
        answer = next((word for word in words if len(word)>=2 and word!=quote), None)
        if answer is None:
            continue
        prompt = cloze_question(quote,answer)
        normalized = re.sub(r"\s+", "",prompt).casefold()
        signature = (re.sub(r"\s+", "",quote).casefold(),re.sub(r"\s+", "",answer).casefold())
        if normalized in seen or signature in seen:
            continue
        seen.update([normalized,signature])
        key = digest([RULE_VERSION,context.model_dump(mode="json"),evidence.block_id,quote,answer])
        questions.append(Question(question_id=key,question=prompt,answer=answer,
            explanation="근거 원문: "+quote,source_block_ids=[evidence.block_id],source_quote=quote))
        if len(questions)==max_questions:
            break
    return QuizBatch(questions=questions)


def plan_local_quiz(source, hierarchy, layer, section_id: str, *, asset_root: Path | None = None,
                    max_questions: int = 1) -> LocalQuizPlan:
    if type(max_questions) is not int or not 1<=max_questions<=3:
        raise ValueError("one to three local review questions supported")
    before = source.model_dump_json()
    context = prepare_context(source,hierarchy,layer,section_id,asset_root)
    saved_context = context.model_dump_json()
    batch = rule_batch(type(context).model_validate_json(saved_context),max_questions)
    # Repeat authoritative preparation; a ready result cannot certify itself.
    current = prepare_context(source,hierarchy,layer,section_id,asset_root)
    if current.model_dump_json()!=saved_context or source.model_dump_json()!=before:
        raise ValueError("source or evidence changed during local question planning")
    errors = verify_quiz(current,batch,max_questions)
    if not batch.questions: errors.append("no_eligible_local_rule_question")
    questions = []
    blocks = {b.block_id:b for b in source.document.blocks}
    evidence = {e.block_id:e for e in current.evidence}
    corrections = {c.block_id:c for c in layer.corrections}
    evidence_digest = hashlib.sha256(saved_context.encode()).hexdigest()
    review_digest = digest([hierarchy.model_dump(mode="json"),layer.model_dump(mode="json")])
    if not errors:
        for question in batch.questions:
            block_id = question.source_block_ids[0]
            block = blocks[block_id]
            correction = corrections.get(block_id)
            entry = dict(question=question.model_dump(mode="json"),source=block.source.model_dump(mode="json"),
                evidence_layer=evidence[block_id].layer,original_text=block.text,effective_text=evidence[block_id].text,
                correction_id=correction.correction_id if correction else None)
            key = digest([RULE_VERSION,current.source_digest,evidence_digest,review_digest,entry])
            questions.append(ReviewedRuleQuestion(operation_key=key,**entry))
    return LocalQuizPlan(status="failed_human_review" if errors else "ready_for_review",
        document_id=current.document_id,version=current.version,section_id=current.section_id,
        source_digest=current.source_digest,evidence_digest=evidence_digest,review_digest=review_digest,
        source_pages=len(source.document.pages),source_blocks=len(source.document.blocks),
        questions=questions,errors=errors,events=["prepare_confirmed_context","local_rule_generate","independent_validate"])
