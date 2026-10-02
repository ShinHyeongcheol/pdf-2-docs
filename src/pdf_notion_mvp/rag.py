"""Local lexical, extractive review answers; no embeddings or semantic grading."""
import re
from copy import deepcopy
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import Field

from .contracts import Contract, FixtureInput, Source
from .notion_quiz import SectionPage, digest
from .review import HierarchicalOutline, ReviewLayer, apply_review

UNKNOWN = "근거를 찾지 못했습니다. 모릅니다."
MAX_INDEX_ENTRIES = 200


class IndexLimitExceeded(ValueError):
    """Select fewer outline leaves; never silently truncate confirmed evidence."""
    pass


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w가-힣]+", text.casefold()))


class RagEvidence(Contract):
    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_id: str
    source_block_id: str
    source: Source
    source_digest: str
    review_digest: str
    text: str = Field(min_length=1, max_length=10_000)
    original_text: str = Field(min_length=1, max_length=10_000)
    layer: Literal["authored_synthetic", "confirmed_transcription"]
    correction_id: str | None
    notion_link: str | None


class RagIndex(Contract):
    mode: Literal["lexical_local"] = "lexical_local"
    document_id: str
    version: str
    source_digest: str
    review_digest: str
    index_digest: str
    entries: list[RagEvidence] = Field(max_length=MAX_INDEX_ENTRIES)
    selected_section_ids: list[str] | None = None
    actual_embeddings_used: Literal[False] = False
    vector_search_implemented: Literal[False] = False


def prepare_index(source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer,
                  bindings: list[SectionPage] | None = None, asset_root: Path | None = None,
                  *, section_ids: list[str] | None = None) -> RagIndex:
    reviewed = apply_review(source, hierarchy, layer, asset_root)
    bindings = [SectionPage.model_validate(b.model_dump()) for b in (bindings or [])]
    sections = {s.section_id for s in reviewed.sections}
    if section_ids is not None and (not section_ids or len(section_ids) != len(set(section_ids)) or
            any(not isinstance(i, str) or i not in sections for i in section_ids)):
        raise ValueError("selected sections must be unique known outline leaves")
    selected = [s.section_id for s in reviewed.sections if s.section_id in section_ids] if section_ids is not None else None
    links = {}
    for binding in bindings:
        if (binding.document_id, binding.version) != (reviewed.original.document_id, reviewed.original.version) or binding.section_id not in sections or binding.section_id in links:
            raise ValueError("RAG section binding mismatch or duplicate")
        links[binding.section_id] = "https://www.notion.so/"+binding.page_id.hex
    review_digest = digest([reviewed.hierarchy.model_dump(mode="json"), reviewed.layer.model_dump(mode="json")])
    corrections = {c.block_id:c for c in reviewed.layer.corrections}
    excluded = {block_id for fragment in reviewed.layer.fragments
                if fragment.kind == "code"
                for block_id in fragment.source_block_ids}
    entries = []
    for section in reviewed.sections:
        if selected is not None and section.section_id not in selected:
            continue
        for block in section.source_blocks:
            if block.kind != "text" or block.role != "body" or block.block_id in excluded:
                continue
            correction = corrections.get(block.block_id)
            if correction and correction.status == "candidate":
                continue
            if source.kind == "ocr_ir" and (correction is None or correction.status != "confirmed"):
                continue
            fields = dict(section_id=section.section_id, source_block_id=block.block_id,
                source=block.source.model_dump(mode="json"), source_digest=reviewed.source_digest,
                review_digest=review_digest, text=section.effective_text[block.block_id], original_text=block.text,
                layer="confirmed_transcription" if correction else "authored_synthetic",
                correction_id=correction.correction_id if correction else None, notion_link=links.get(section.section_id))
            if len(entries) >= MAX_INDEX_ENTRIES:
                raise IndexLimitExceeded("selected confirmed text exceeds 200 entries; choose fewer leaves")
            entries.append(RagEvidence(evidence_id=digest(["rag-evidence-v1",fields]), **fields))
    value = dict(document_id=reviewed.original.document_id, version=reviewed.original.version,
        source_digest=reviewed.source_digest, review_digest=review_digest, entries=[e.model_dump(mode="json") for e in entries], selected_section_ids=selected)
    return RagIndex(index_digest=digest(["rag-index-v1",value]), **value)


class LexicalRetriever(BaseRetriever):
    """LangChain retriever: deterministic token overlap; zero-vector local baseline."""
    documents: list[Document]
    limit: int = Field(default=3, ge=1, le=3)

    def _get_relevant_documents(self, query: str, *, run_manager):
        terms = tokens(query)
        ranked = [(len(terms & tokens(d.page_content)), i, d) for i,d in enumerate(self.documents)]
        ranked.sort(key=lambda row:(-row[0],row[1]))
        return [d.model_copy(deep=True) for score,_,d in ranked[:self.limit] if score > 0]


class RetrievalAdapter(Protocol):
    # A future embedding/vector implementation replaces this boundary and needs a new gate.
    mode: str
    def build(self, index: RagIndex) -> BaseRetriever: ...


class LocalLexicalAdapter:
    mode = "lexical_local"
    def build(self, index: RagIndex) -> BaseRetriever:
        return LexicalRetriever(documents=[Document(page_content=e.text, metadata={"evidence":e.model_dump(mode="json")}) for e in index.entries])


class RagContext(Contract):
    question: str = Field(min_length=1, max_length=1000)
    index_digest: str
    retrieved: list[RagEvidence] = Field(max_length=3)


class Citation(Contract):
    evidence_id: str
    index_digest: str
    source_block_id: str
    section_id: str
    source: Source
    source_digest: str
    review_digest: str
    layer: Literal["authored_synthetic", "confirmed_transcription"]
    correction_id: str | None
    notion_link: str | None
    quote: str = Field(min_length=1, max_length=10_000)


def citation_for(entry: RagEvidence, index_digest: str) -> Citation:
    data = entry.model_dump(exclude={"text", "original_text"})
    return Citation(**data, index_digest=index_digest, quote=entry.text)


class RagDraft(Contract):
    kind: Literal["answer", "unknown"]
    answer: str = Field(min_length=1, max_length=40_000)
    citations: list[Citation] = Field(max_length=3)


def extractive_draft(context: RagContext) -> RagDraft:
    if not context.retrieved:
        return RagDraft(kind="unknown", answer=UNKNOWN, citations=[])
    citations = [citation_for(e,context.index_digest) for e in context.retrieved]
    return RagDraft(kind="answer", answer="근거 원문:\n"+"\n".join(c.quote for c in citations), citations=citations)


class AnswerAdapter(Protocol):
    mode: str
    def generate(self, context: RagContext) -> object: ...


class MockAnswerAdapter:
    mode = "mock"
    def __init__(self, response=None):
        self.response = response
        self.calls = 0
        self.chain = RunnableLambda(self.respond)
    def respond(self, context):
        self.calls += 1
        if isinstance(self.response, Exception): raise self.response
        return extractive_draft(context) if self.response is None else deepcopy(self.response)
    def generate(self, context): return self.chain.invoke(context)


def verify_answer(context: RagContext, draft: RagDraft) -> list[str]:
    context = RagContext.model_validate(context.model_dump())
    draft = RagDraft.model_validate(draft.model_dump())
    if not context.retrieved:
        return [] if draft == extractive_draft(context) else ["unsupported_answer_without_evidence"]
    errors = []
    authority = {e.evidence_id:e for e in context.retrieved}
    ids = [c.evidence_id for c in draft.citations]
    if draft.kind != "answer" or not ids or len(ids) != len(set(ids)):
        errors.append("answer_or_citation_count")
    for citation in draft.citations:
        entry = authority.get(citation.evidence_id)
        if entry is None or citation != citation_for(entry, context.index_digest):
            errors.append("unsupported_quote_or_provenance")
    if draft.answer != "근거 원문:\n"+"\n".join(c.quote for c in draft.citations):
        errors.append("unsupported_answer_claim")
    return sorted(set(errors))


class RagResult(Contract):
    status: Literal["ready_for_review", "unknown", "failed_human_review"]
    retrieval_mode: Literal["lexical_local"] = "lexical_local"
    provider_mode: Literal["mock"] = "mock"
    question: str
    document_id: str
    version: str
    source_digest: str
    index_digest: str
    retrieved_ids: list[str]
    indexed_entries: int = Field(ge=0, le=MAX_INDEX_ENTRIES)
    selected_section_ids: list[str] | None = None
    draft: RagDraft | None
    errors: list[str]
    events: list[str]
    generation_skipped: bool
    actual_embeddings_used: Literal[False] = False
    vector_search_implemented: Literal[False] = False
    human_review_required: Literal[True] = True
    semantic_correctness_verified: Literal[False] = False
    notion_links_verified: Literal[False] = False


class RagState(TypedDict):
    authority_json: str
    question: str
    context_json: str | None
    draft: RagDraft | None
    errors: list[str]
    events: list[str]
    generation_skipped: bool


class RagWorkflow:
    def __init__(self, generator: AnswerAdapter | None = None, retrieval: RetrievalAdapter | None = None):
        self.generator = generator if generator is not None else MockAnswerAdapter()
        self.retrieval = retrieval if retrieval is not None else LocalLexicalAdapter()
        graph = StateGraph(RagState)
        graph.add_node("retrieve",self.retrieve)
        graph.add_node("generate",self.generate)
        graph.add_node("independent_verify",self.verify)
        graph.add_edge(START,"retrieve")
        graph.add_edge("retrieve","generate")
        graph.add_edge("generate","independent_verify")
        graph.add_edge("independent_verify",END)
        self.graph = graph.compile()

    def retrieve(self,state):
        updated = dict(state,events=state["events"]+["retrieve"])
        try:
            if self.retrieval.mode != "lexical_local": raise ValueError("embedding/vector calls are disabled")
            index = RagIndex.model_validate_json(state["authority_json"])
            retriever = self.retrieval.build(index.model_copy(deep=True))
            if not isinstance(retriever,BaseRetriever): raise ValueError("LangChain retriever required")
            documents = retriever.invoke(state["question"],config={"callbacks":[]})
            if len(documents)>3: raise ValueError("retrieval limit exceeded")
            by_id = {e.evidence_id:e for e in index.entries}
            entries,seen = [],set()
            for document in documents:
                entry = RagEvidence.model_validate(document.metadata["evidence"])
                if (entry.evidence_id in seen or entry != by_id.get(entry.evidence_id) or
                    document.page_content != entry.text or not (tokens(state["question"]) & tokens(entry.text))):
                    raise ValueError("retrieved evidence is not authoritative or relevant")
                seen.add(entry.evidence_id)
                entries.append(entry)
            updated["context_json"] = RagContext(question=state["question"],index_digest=index.index_digest,retrieved=entries).model_dump_json()
        except Exception as exc:
            updated["errors"] = ["retrieve_error:"+type(exc).__name__]
        return updated

    def generate(self,state):
        updated = dict(state,events=state["events"]+["generate"])
        if state["errors"]:
            return dict(updated,generation_skipped=True)
        context = RagContext.model_validate_json(state["context_json"])
        if not context.retrieved:
            return dict(updated,draft=extractive_draft(context),generation_skipped=True)
        try:
            if self.generator.mode != "mock": raise ValueError("actual answer model calls are disabled")
            value = self.generator.generate(context.model_copy(deep=True))
            updated["draft"] = RagDraft.model_validate_json(value) if isinstance(value,str) else RagDraft.model_validate(value.model_dump() if isinstance(value,RagDraft) else value)
        except Exception as exc:
            updated["errors"] = ["generate_error:"+type(exc).__name__]
        return updated

    @staticmethod
    def verify(state):
        errors = state["errors"] if state["draft"] is None else verify_answer(RagContext.model_validate_json(state["context_json"]),state["draft"])
        return dict(state,errors=errors,events=state["events"]+["independent_verify"])

    def run(self, source, hierarchy, layer, question, bindings=None, *, index=None, asset_root=None, section_ids=None):
        if not isinstance(question,str) or not question.strip() or len(question)>1000:
            raise ValueError("bounded nonempty question required")
        current = prepare_index(source,hierarchy,layer,bindings,asset_root,section_ids=section_ids)
        if index is not None and RagIndex.model_validate(index.model_dump()) != current:
            raise ValueError("saved index is stale or mismatched; rebuild from current source")
        initial = dict(authority_json=current.model_dump_json(), question=question, context_json=None,
            draft=None,errors=[],events=[],generation_skipped=False)
        with tracing_context(enabled=False):
            state = self.graph.invoke(initial,config={"recursion_limit":8,"callbacks":[]})
        context = RagContext.model_validate_json(state["context_json"]) if state["context_json"] else None
        status = "failed_human_review" if state["errors"] else "unknown" if state["draft"].kind=="unknown" else "ready_for_review"
        return RagResult(status=status,question=question,document_id=current.document_id,version=current.version,
            source_digest=current.source_digest,index_digest=current.index_digest,retrieved_ids=[e.evidence_id for e in context.retrieved] if context else [],
            indexed_entries=len(current.entries),selected_section_ids=current.selected_section_ids,
            draft=None if state["errors"] else state["draft"],errors=state["errors"],events=state["events"],generation_skipped=state["generation_skipped"])
