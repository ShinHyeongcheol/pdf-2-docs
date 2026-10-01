"""Convert local Apple Vision OCR results to source-preserving IR."""
import hashlib
from pathlib import Path

from pydantic import Field

from .contracts import (
    Box, Contract, DocumentIR, ExtractionInfo, FixtureInput,
    ImageBlock, Page, Source, TextBlock,
)


class OCRLine(Contract):
    text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    x0: float = Field(ge=0, le=1, allow_inf_nan=False)
    y0: float = Field(ge=0, le=1, allow_inf_nan=False)
    x1: float = Field(ge=0, le=1, allow_inf_nan=False)
    y1: float = Field(ge=0, le=1, allow_inf_nan=False)


class OCRPage(Contract):
    engine: str
    languages: list[str]
    lines: list[OCRLine]


def ocr_to_ir(name: str, document_sha256: str, pages: list[Page], results: dict[int, OCRPage], assets: dict[int, Path]) -> FixtureInput:
    document_id = f"pdf-{document_sha256[:16]}"
    version = document_sha256
    blocks = []
    warnings = [
        "OCR candidate text needs human review; confidence is not proof of correctness.",
        "Tables, diagrams and code remain preserved in page rasters; semantic reconstruction is not implemented.",
    ]
    for page in pages:
        result = results.get(page.number)
        if result is None:
            continue
        asset = assets[page.number]
        for index, line in enumerate(result.lines):
            box = Box(x0=line.x0 * page.width, y0=line.y0 * page.height,
                      x1=line.x1 * page.width, y1=line.y1 * page.height)
            blocks.append(TextBlock(
                block_id=f"p{page.number:04d}-ocr-{index + 1:04d}", text=line.text,
                role="heading" if index == 0 else "body",
                source=Source(document_id=document_id, version=version, page=page.number,
                              bbox=box, method="ocr", confidence=line.confidence),
            ))
        if not result.lines:
            warnings.append(f"page {page.number}: no text recognized; page raster retained")
            blocks.append(TextBlock(
                block_id=f"p{page.number:04d}-heading", text=f"Page {page.number} — no OCR text",
                role="heading", source=Source(document_id=document_id, version=version,
                    page=page.number, bbox=Box(x0=0,y0=0,x1=page.width,y1=page.height), method="raster"),
            ))
        blocks.append(ImageBlock(
            block_id=f"p{page.number:04d}-raster", asset_ref=str(asset.resolve()),
            sha256=hashlib.sha256(asset.read_bytes()).hexdigest(), caption=f"Original page {page.number} raster",
            source=Source(document_id=document_id, version=version, page=page.number,
                          bbox=Box(x0=0,y0=0,x1=page.width,y1=page.height), method="raster"),
        ))
    document = DocumentIR(
        document_id=document_id, version=version, name=name, pages=pages, blocks=blocks,
        extraction=ExtractionInfo(engine="apple-vision-r3", pages_processed=sorted(results),
                                  human_review_required=True, warnings=warnings),
    )
    return FixtureInput(kind="ocr_ir", document=document)
