# Native PDF local path and SDK validation boundary

A new authored two-page PDF now goes through real local extraction and raster rendering, durable jobs, explicit review, mocked lessons/questions, local citation retrieval and mocked Notion publication. No key, model request, embedding request or live Notion write is needed for this test.

```sh
python -m pip install -e '.[test,pdf]'
python -m pytest tests/test_native_pdf.py -q
python -m pdf_notion_mvp.native_pdf input.pdf --assets output/native/assets --output output/native/source.json
python -m pdf_notion_mvp.cli output/native/source.json --db output/native/jobs.sqlite --output output/native/job.json
```

Keep assets under the source JSON directory. File-based review/RAG loaders trust that directory; the API requires `PDF_NOTION_ASSET_ROOT` set on the server. The API accepts the extracted `pdf_ir` JSON, rather than a PDF upload or arbitrary file path. Missing, changed or out-of-root page PNGs block the publishing plan. Raster installation refuses symlink directories/files, hard-linked files and mismatched existing caches. An identical PNG is reused without overwrite. CLI output cannot overwrite the original PDF or its PNGs. A `ready` job means the original IR was preserved; it does not certify semantic reading order or authorize model/network operations.

Supported extraction: unencrypted, unrotated text-only pages with resolved font characters. The document version is the SHA-256 of the same PDF byte snapshot used to extract and render. Every line has its page and PDF-space coordinates, and every page has an independently hashed full-page PNG. Mixed/image/empty pages are rejected and must use the existing Mac OCR/review path. Font mapping failures also require review. Multi-column reading order, headings, table cells and code indentation need explicit review. Raw line text is retained; confirmed table/code groups belong in the review layer, never an invented native extraction confidence.

The tests cover two actual pages, 11 native text lines, two real PNGs, five job transitions, original numeric values, explicit table/code grouping, restart without re-extraction, missing raster rejection, mock lesson citation validation, unknown-answer handling and lost mock publication response reconciliation with zero duplicate writes. The source lesson responses are authored fixtures, not cached responses from an unrelated document.

The installed Google SDK was also exercised independently using a fabricated key and `httpx.MockTransport`: `ChatGoogleGenerativeAI.invoke` and `stream`, JSON/SSE parsing, input/output token metadata and `STOP`. That proves local SDK serialization and parsing. It does not prove live provider behavior, model quality, real token charges or execution of the PDF's printed sample code.

Before a live SDK request: bind the reviewed text scope, model/pricing snapshot, input/output limits and expiry to the request digest; reserve against the existing cumulative budget before key lookup; disable automatic retries, tracing and tool calls; record at most the approved request count; verify finish reason, usage and source citations; reconcile uncertain outcomes instead of retrying blindly. Existing approved receipts can be replayed only for their exact input and operation identity. New PDF content needs its own bounded generation plan. PDF images require separate explicit authorization. This addition makes no live SDK calls and does not reset the budget ledger.

The extraction boundary follows [pdfplumber's native text and rendering API](https://github.com/jsvine/pdfplumber). Current SDK behavior above is grounded in the installed package transport tests rather than a promise about future package versions.
