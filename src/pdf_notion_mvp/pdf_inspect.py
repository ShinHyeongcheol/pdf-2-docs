"""Read-only real PDF inspection. Never treats a scan as extracted text."""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract


class PageInspection(Contract):
    page: int = Field(ge=1)
    width: float
    height: float
    text_chars: int = Field(ge=0)
    image_count: int = Field(ge=0)
    ocr_candidate: bool
    content_type: Literal["image_only", "text_only", "mixed", "empty"]


class PDFInspection(Contract):
    sha256: str
    pages: list[PageInspection]
    page_count: int
    text_chars: int
    image_count: int
    ocr_required: bool | None
    ready_for_text_extraction: bool
    warnings: list[str]


def inspect_pdf(path: Path) -> PDFInspection:
    # Optional dependency: keep the synthetic path small.
    import pdfplumber

    if not path.is_file():
        raise FileNotFoundError(path)
    with pdfplumber.open(path) as pdf:
        pages = []
        for page in pdf.pages:
            chars = len((page.extract_text() or "").strip())
            pages.append(PageInspection(
                page=page.page_number, width=page.width, height=page.height,
                text_chars=chars, image_count=len(page.images),
                ocr_candidate=chars == 0 and bool(page.images),
                content_type="mixed" if chars and page.images else "text_only" if chars else "image_only" if page.images else "empty",
            ))
    if not pages:
        raise ValueError("PDF has no pages")
    ocr = any(p.ocr_candidate for p in pages)
    uncertain = any(p.content_type in {"mixed", "empty"} for p in pages)
    total_chars = sum(p.text_chars for p in pages)
    return PDFInspection(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), pages=pages,
        page_count=len(pages), text_chars=total_chars,
        image_count=sum(p.image_count for p in pages), ocr_required=True if ocr else None if uncertain else False,
        ready_for_text_extraction=total_chars > 0 and not ocr and not uncertain,
        warnings=(["Image-only pages require OCR."] if ocr else []) + (["Mixed/empty pages require review; text may be only a watermark over scanned content."] if uncertain else []),
    )


def main():
    parser = argparse.ArgumentParser(description="Inspect a real PDF locally without OCR or model calls")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/pdf-inspection.json"))
    args = parser.parse_args()
    result = inspect_pdf(args.pdf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps(result.model_dump(exclude={"pages"}), ensure_ascii=False))
    raise SystemExit(0 if result.ready_for_text_extraction else 2)


if __name__ == "__main__":
    main()
