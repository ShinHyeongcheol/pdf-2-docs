"""Deterministic verifier, independent of planning/restoration adapters."""
import hashlib
from pathlib import Path

from .contracts import DocumentIR, ImageBlock, Outline, RestoredDocument, ValidationReport


def verify(document: DocumentIR, outline: Outline, restored: RestoredDocument, asset_root: Path | None = None) -> ValidationReport:
    errors = []
    if document.extraction.engine != "synthetic" and sorted(document.extraction.pages_processed) != [p.number for p in document.pages]:
        errors.append("extraction coverage is incomplete; publishing plan is blocked")
    if document.extraction.engine != "synthetic":
        observed_pages = {b.source.page for b in document.blocks}
        expected_pages = {p.number for p in document.pages}
        if observed_pages != expected_pages:
            errors.append("actual block page coverage is incomplete")
        rasters = [b for b in document.blocks if isinstance(b, ImageBlock) and b.source.method == "raster"]
        if len({b.asset_ref for b in rasters}) != len(rasters):
            errors.append("page rasters must have unique asset references")
        for page in document.pages:
            page_rasters = [b for b in rasters if b.source.page == page.number]
            if len(page_rasters) != 1:
                errors.append(f"page {page.number} must have exactly one source raster")
                continue
            raster = page_rasters[0]
            box = raster.source.bbox
            if (box.x0,box.y0,box.x1,box.y1) != (0,0,page.width,page.height):
                errors.append(f"page {page.number} raster must cover full page")
            if asset_root is None:
                errors.append("trusted asset root is required for OCR verification")
                continue
            path = Path(raster.asset_ref).resolve()
            if not path.is_relative_to(asset_root.resolve()):
                errors.append(f"page {page.number} raster is outside trusted asset root")
            elif not path.is_file():
                errors.append(f"page {page.number} raster asset is missing")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != raster.sha256:
                errors.append(f"page {page.number} raster asset digest mismatch")
    expected = [b.block_id for b in document.blocks]
    planned = [bid for s in outline.sections for bid in s.block_ids]
    actual = [b.block_id for s in restored.sections for b in s.blocks]
    if planned != expected:
        errors.append("outline coverage/order differs from IR")
    if actual != expected:
        errors.append("restored coverage/order differs from IR (missing, duplicate or reordered blocks)")
    if len({s.section_id for s in outline.sections}) != len(outline.sections):
        errors.append("outline section IDs are duplicated")
    planned_sections = [(s.section_id, s.title, s.block_ids) for s in outline.sections]
    actual_sections = [(s.section_id, s.title, [b.block_id for b in s.blocks]) for s in restored.sections]
    if planned_sections != actual_sections:
        errors.append("restored section structure differs from outline")
    source_blocks = {b.block_id: b for b in document.blocks}
    for section in restored.sections:
        for block in section.blocks:
            original = source_blocks.get(block.block_id)
            if original is None or block.model_dump() != original.model_dump():
                errors.append(f"content/type/provenance differs for block {block.block_id}")
    return ValidationReport(
        passed=not errors,
        checks=["extraction_page_coverage", "actual_page_rasters_and_assets", "outline_coverage_order", "restored_coverage_order", "unique_sections", "section_structure", "content_and_provenance"],
        errors=errors,
    )
