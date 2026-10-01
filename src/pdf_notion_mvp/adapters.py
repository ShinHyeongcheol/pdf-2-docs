"""Replaceable boundaries. Every default adapter is local and deterministic."""
from typing import Protocol

from langchain_core.runnables import RunnableLambda

from .contracts import (
    DocumentIR, FixtureInput, Outline, PublishOperation, PublishPlan,
    RestoredDocument, RestoredSection, Section, TextBlock,
)
from .identity import operation_key


class Extractor(Protocol):
    def extract(self, source: FixtureInput) -> DocumentIR: ...


class KnowledgeAdapter(Protocol):
    def plan(self, document: DocumentIR) -> Outline: ...
    def restore(self, document: DocumentIR, outline: Outline) -> RestoredDocument: ...


class PublishPlanner(Protocol):
    def plan(self, restored: RestoredDocument, fingerprint: str) -> PublishPlan: ...


class FixtureExtractor:
    def extract(self, source: FixtureInput) -> DocumentIR:
        # Revalidate at the boundary, rather than trusting a mutable model instance.
        return DocumentIR.model_validate(source.document.model_dump())


def heading_plan(document: DocumentIR) -> dict:
    sections: list[Section] = []
    for block in document.blocks:
        is_heading = isinstance(block, TextBlock) and block.role == "heading"
        if not sections or is_heading:
            sections.append(Section(
                section_id=f"section-{len(sections) + 1}",
                title=block.text if is_heading else document.name,
                block_ids=[block.block_id],
            ))
        else:
            sections[-1].block_ids.append(block.block_id)
    return {"sections": [s.model_dump() for s in sections]}


def restore_sections(value: tuple[DocumentIR, Outline]) -> dict:
    document, outline = value
    blocks = {block.block_id: block for block in document.blocks}
    return {"sections": [RestoredSection(
        section_id=s.section_id, title=s.title,
        blocks=[blocks[block_id].model_copy(deep=True) for block_id in s.block_ids],
    ).model_dump() for s in outline.sections]}


class LocalKnowledgeAdapter:
    """Real LCEL execution, with mock content logic and no model/provider."""

    def __init__(self):
        self.planning_chain = RunnableLambda(heading_plan) | RunnableLambda(Outline.model_validate)
        self.restoration_chain = RunnableLambda(restore_sections) | RunnableLambda(RestoredDocument.model_validate)

    def plan(self, document: DocumentIR) -> Outline:
        return self.planning_chain.invoke(document)

    def restore(self, document: DocumentIR, outline: Outline) -> RestoredDocument:
        return self.restoration_chain.invoke((document, outline))


class DryRunNotionPlanner:
    """Neutral typed plan, not a validated Notion HTTP request or writer."""

    def plan(self, restored: RestoredDocument, fingerprint: str) -> PublishPlan:
        operations = []
        for section in restored.sections:
            for block in section.blocks:
                key = operation_key(fingerprint, section.section_id, block.block_id)
                operations.append(PublishOperation(
                    operation_key=key, section_id=section.section_id,
                    section_title=section.title, block=block.model_copy(deep=True),
                ))
        return PublishPlan(operations=operations)
