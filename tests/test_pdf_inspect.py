import os
from pathlib import Path

import pytest

from pdf_notion_mvp.pdf_inspect import inspect_pdf


def test_private_real_pdf_if_supplied():
    path = os.environ.get("PDF_NOTION_TEST_PDF")
    if not path:
        pytest.skip("set PDF_NOTION_TEST_PDF to a local private PDF for read-only integration")
    pytest.importorskip("pdfplumber")
    result = inspect_pdf(Path(path))
    assert result.page_count > 0
    assert len(result.pages) == result.page_count
    assert result.text_chars == sum(p.text_chars for p in result.pages)
    assert result.image_count == sum(p.image_count for p in result.pages)
    if any(p.ocr_candidate for p in result.pages):
        assert result.ocr_required is True
    if result.ocr_required is not False:
        assert not result.ready_for_text_extraction
        assert result.warnings


def test_watermark_over_scan_is_unknown(tmp_path, monkeypatch):
    pdfplumber = pytest.importorskip("pdfplumber")
    class FakePage:
        page_number = 1
        width, height = 100, 100
        images = [{"synthetic": True}]
        def extract_text(self): return "synthetic watermark"
    class FakePDF:
        pages = [FakePage()]
        def __enter__(self): return self
        def __exit__(self, *args): pass
    path = tmp_path / "placeholder.bin"
    path.write_bytes(b"synthetic test input; not a PDF")
    monkeypatch.setattr(pdfplumber, "open", lambda _: FakePDF())
    result = inspect_pdf(path)
    assert result.pages[0].content_type == "mixed"
    assert result.ocr_required is None and not result.ready_for_text_extraction
    assert "watermark" in result.warnings[0]
