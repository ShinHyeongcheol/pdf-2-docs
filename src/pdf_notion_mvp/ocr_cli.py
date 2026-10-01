"""Optional Mac-local OCR command; renders/caches only in the chosen private directory."""
import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path

from .contracts import Page
from .ocr import OCRPage, ocr_to_ir


def main():
    parser = argparse.ArgumentParser(description="Local macOS OCR to candidate IR, with page raster provenance")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--vision-binary", required=True, type=Path)
    parser.add_argument("--pages", default="all", help="all, or comma-separated source page numbers")
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("Apple Vision OCR requires macOS")
    if not 72 <= args.dpi <= 300:
        parser.error("dpi must be between 72 and 300")
    import pdfplumber

    digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    cache = args.work_dir.resolve() / digest / f"apple-vision-r3-{args.dpi}dpi"
    cache.mkdir(parents=True, exist_ok=True)
    results, assets, pages = {}, {}, []
    with pdfplumber.open(args.pdf) as pdf:
        selected = set(range(1, len(pdf.pages) + 1)) if args.pages == "all" else {int(n) for n in args.pages.split(",")}
        if not selected or not selected.issubset(set(range(1, len(pdf.pages) + 1))):
            parser.error("selected pages must exist in the PDF")
        for page in pdf.pages:
            number = page.page_number
            pages.append(Page(number=number, width=page.width, height=page.height))
            if number not in selected:
                continue
            raster = cache / f"page-{number:04d}.png"
            result = cache / f"page-{number:04d}.json"
            if not raster.exists():
                page.to_image(resolution=args.dpi).save(raster)
            if not result.exists():
                subprocess.run([str(args.vision_binary.resolve()), str(raster), str(result)], check=True, timeout=60, capture_output=True)
            results[number] = OCRPage.model_validate_json(result.read_text())
            assets[number] = raster
            if number % 10 == 0 or number == min(selected) or number == max(selected):
                print(f"processed page={number} lines={len(results[number].lines)}", flush=True)
    source = ocr_to_ir(args.pdf.name, digest, pages, results, assets)
    output = cache / ("document-ir.json" if args.pages == "all" else "sample-ir.json")
    output.write_text(source.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"output":str(output), "pages_processed":len(results), "total_pages":len(pages),
                      "blocks":len(source.document.blocks), "human_review_required":True},ensure_ascii=False))


if __name__ == "__main__":
    main()
