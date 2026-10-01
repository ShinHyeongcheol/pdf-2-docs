"""Local hierarchy and review overlays; source blocks are never rewritten."""
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .contracts import (
    Block, Box, Contract, DocumentIR, Outline, Section, Source, TextBlock,
)


def document_digest(document: DocumentIR) -> str:
    return hashlib.sha256(document.model_dump_json().encode()).hexdigest()


class OutlineNode(Contract):
    node_id: str = Field(min_length=1)
    parent_id: str | None = None
    title: str = Field(min_length=1)
    source_pages: list[int] = Field(min_length=1)
    block_ids: list[str] = Field(default_factory=list)
    review_status: Literal["candidate", "confirmed"] = "candidate"


class HierarchicalOutline(Contract):
    document_id: str
    version: str
    nodes: list[OutlineNode] = Field(min_length=1)

    @model_validator(mode="after")
    def tree_structure(self):
        seen = set()
        for node in self.nodes:
            if node.node_id in seen:
                raise ValueError("duplicate outline node ID")
            if node.parent_id is not None and node.parent_id not in seen:
                raise ValueError("parent must precede child; cycles are forbidden")
            if len(set(node.source_pages)) != len(node.source_pages) or node.source_pages != sorted(node.source_pages):
                raise ValueError("source pages must be sorted and unique")
            seen.add(node.node_id)
        children = {n.parent_id for n in self.nodes if n.parent_id is not None}
        for node in self.nodes:
            if bool(node.block_ids) == (node.node_id in children):
                raise ValueError("only leaves own blocks; every leaf must own blocks")
        return self


def validate_outline(document: DocumentIR, outline: HierarchicalOutline) -> Outline:
    # Revalidate mutable instances at public boundaries.
    document = DocumentIR.model_validate(document.model_dump())
    outline = HierarchicalOutline.model_validate(outline.model_dump())
    if (outline.document_id, outline.version) != (document.document_id, document.version):
        raise ValueError("outline source mismatch")
    children = {}
    for node in outline.nodes:
        children.setdefault(node.parent_id, []).append(node)
    traversal, leaves = [], []
    def walk(node):
        traversal.append(node.node_id)
        descendants = children.get(node.node_id, [])
        if descendants:
            pages = set()
            for child in descendants:
                pages.update(walk(child))
        else:
            leaves.append(node)
            pages = {blocks[i].source.page for i in node.block_ids if i in blocks}
            if any(i not in blocks for i in node.block_ids):
                raise ValueError("unknown source block")
        if sorted(pages) != node.source_pages:
            raise ValueError("declared pages differ from source coverage")
        return pages
    blocks = {b.block_id: b for b in document.blocks}
    covered_pages = set()
    for root in children[None]:
        covered_pages.update(walk(root))
    if traversal != [n.node_id for n in outline.nodes]:
        raise ValueError("nodes must follow tree preorder")
    ids = [i for leaf in leaves for i in leaf.block_ids]
    if ids != [b.block_id for b in document.blocks]:
        raise ValueError("leaf blocks must cover source exactly once in input IR order")
    if covered_pages != {p.number for p in document.pages}:
        raise ValueError("outline must cover every source page")
    return Outline(sections=[Section(section_id=n.node_id, title=n.title, block_ids=n.block_ids) for n in leaves])


class Correction(Contract):
    correction_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    original_text: str = Field(min_length=1)
    source: Source
    proposed_text: str = Field(min_length=1)
    status: Literal["candidate", "confirmed"]
    basis: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    evidence_block_id: str = Field(min_length=1)
    evidence_bbox: Box


class DerivedFragment(Contract):
    fragment_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)
    kind: Literal["text", "code", "table"]
    text: str = Field(min_length=1)
    status: Literal["candidate", "confirmed"]
    basis: str = Field(min_length=1)
    # For grouped code/table layouts, applied corrections must be traceable.
    correction_ids: list[str] = Field(default_factory=list)


class ReviewLayer(Contract):
    document_id: str
    version: str
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    corrections: list[Correction] = Field(default_factory=list)
    fragments: list[DerivedFragment] = Field(default_factory=list)


class ReviewedSection(Contract):
    section_id: str
    title: str
    source_blocks: list[Block]
    # Each effective text is separate from its unchanged source block.
    effective_text: dict[str, str]
    confirmed_fragments: list[DerivedFragment]
    candidate_fragments: list[DerivedFragment]
    pending_corrections: list[Correction]


class ReviewResult(Contract):
    mode: Literal["local_review"] = "local_review"
    source_digest: str
    original: DocumentIR
    hierarchy: HierarchicalOutline
    layer: ReviewLayer
    sections: list[ReviewedSection]
    checks: list[str]
    human_review_required: Literal[True] = True
    semantic_reading_order_verified: Literal[False] = False


def apply_review(document: DocumentIR, hierarchy: HierarchicalOutline, layer: ReviewLayer, asset_root: Path | None = None) -> ReviewResult:
    before = document.model_dump()
    original = DocumentIR.model_validate(before)
    hierarchy = HierarchicalOutline.model_validate(hierarchy.model_dump())
    layer = ReviewLayer.model_validate(layer.model_dump())
    outline = validate_outline(original, hierarchy)
    from .adapters import restore_sections
    from .contracts import RestoredDocument
    from .verification import verify
    report = verify(original, outline, RestoredDocument.model_validate(restore_sections((original, outline))), asset_root)
    if not report.passed:
        raise ValueError("source preservation/assets verification failed: " + "; ".join(report.errors))
    if (layer.document_id, layer.version, layer.source_digest) != (original.document_id, original.version, document_digest(original)):
        raise ValueError("review layer source/version/digest mismatch")
    blocks = {b.block_id: b for b in original.blocks}
    corrections, correction_ids = {}, {}
    for correction in layer.corrections:
        block = blocks.get(correction.block_id)
        if not isinstance(block, TextBlock):
            raise ValueError("correction requires a source text block")
        if correction.block_id in corrections or correction.correction_id in correction_ids:
            raise ValueError("duplicate correction target or ID")
        if correction.original_text != block.text or correction.source != block.source:
            raise ValueError("correction original snapshot mismatch")
        evidence = blocks.get(correction.evidence_block_id)
        if evidence is None or evidence.kind != "image" or evidence.source.page != block.source.page:
            raise ValueError("correction needs a same-page source image")
        e, s, image_box = correction.evidence_bbox, block.source.bbox, evidence.source.bbox
        if not (image_box.x0 <= e.x0 <= s.x0 < s.x1 <= e.x1 <= image_box.x1 and image_box.y0 <= e.y0 <= s.y0 < s.y1 <= e.y1 <= image_box.y1):
            raise ValueError("evidence region must include source text and lie within image")
        corrections[correction.block_id] = correction
        correction_ids[correction.correction_id] = correction
    section_ids = {s.section_id: set(s.block_ids) for s in outline.sections}
    seen_fragments = set()
    for fragment in layer.fragments:
        if fragment.fragment_id in seen_fragments:
            raise ValueError("duplicate fragment ID")
        seen_fragments.add(fragment.fragment_id)
        ids = fragment.source_block_ids
        if len(set(ids)) != len(ids) or not set(ids).issubset(section_ids.get(fragment.section_id, set())):
            raise ValueError("fragment lineage must belong to one outline leaf")
        if len(set(fragment.correction_ids)) != len(fragment.correction_ids):
            raise ValueError("duplicate fragment correction ID")
        for cid in fragment.correction_ids:
            c = correction_ids.get(cid)
            if c is None or c.block_id not in ids or (fragment.status == "confirmed" and c.status != "confirmed"):
                raise ValueError("fragment correction lineage/status mismatch")
    sections = []
    for section in outline.sections:
        owned = [blocks[i].model_copy(deep=True) for i in section.block_ids]
        effective = {}
        for block in owned:
            if isinstance(block, TextBlock):
                correction = corrections.get(block.block_id)
                effective[block.block_id] = correction.proposed_text if correction and correction.status == "confirmed" else block.text
        fragments = [f for f in layer.fragments if f.section_id == section.section_id]
        sections.append(ReviewedSection(
            section_id=section.section_id, title=section.title, source_blocks=owned,
            effective_text=effective,
            confirmed_fragments=[f for f in fragments if f.status == "confirmed"],
            candidate_fragments=[f for f in fragments if f.status == "candidate"],
            pending_corrections=[c for c in layer.corrections if c.block_id in section.block_ids and c.status == "candidate"],
        ))
    if document.model_dump() != before:
        raise ValueError("source mutated during review")
    result = ReviewResult(source_digest=document_digest(original), original=original, hierarchy=hierarchy, layer=layer, sections=sections,
        checks=["source_version_and_digest", "all_pages_covered", "blocks_covered_once", "input_ir_order_preserved", "source_unmodified", "correction_snapshot_and_visual_lineage", "candidates_not_applied", "fragment_lineage", "source_asset_verification"])
    return ReviewResult.model_validate(result.model_dump())
