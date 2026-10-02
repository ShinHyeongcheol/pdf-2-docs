"""Local native text PDF ingestion; page rasters remain the review authority."""
import argparse
import hashlib
import os
import stat
from uuid import uuid4
from pathlib import Path

from .contracts import DocumentIR, ExtractionInfo, FixtureInput, ImageBlock, Page, Source, TextBlock, Box


def _store_raster(directory: Path, digest: str, name: str, data: bytes) -> Path:
    """Install derived PNGs exclusively; never follow or overwrite existing links."""
    directory = directory.absolute()
    if '..' in directory.parts or any(p.is_symlink() for p in [directory, *directory.parents]):
        raise ValueError('asset directories must not contain symlinks or parent traversal')
    directory.mkdir(parents=True, exist_ok=True)
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    folder = None
    temporary = None
    try:
        try:
            os.mkdir(digest, dir_fd=root)
        except FileExistsError:
            pass
        try:
            folder = os.open(digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError as exc:
            raise ValueError('asset version directory must be a real directory') from exc
        temporary = '.raster-' + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        except FileExistsError:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            except OSError as exc:
                raise ValueError('existing raster must not be a symlink') from exc
            with os.fdopen(fd, 'rb') as existing:
                info = os.fstat(existing.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or existing.read(len(data) + 1) != data:
                    raise ValueError('existing raster differs or is hard-linked; refusing overwrite')
    finally:
        if folder is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    pass
            os.close(folder)
        os.close(root)
    return directory / digest / name


def extract_native_pdf(path: Path, asset_dir: Path) -> FixtureInput:
    """Extract line text and coordinates, without claiming semantic table/code recovery.

    Supports unencrypted, unrotated text-only PDFs. Rejects mixed/image/empty pages;
    these require the separate OCR/review path. Headings remain explicit review work.
    """
    import io
    import pdfplumber

    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    document_id = 'pdf-' + digest[:16]
    pages, blocks = [], []
    # Read and render the same byte snapshot used for the version hash.
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if not pdf.pages:
            raise ValueError('PDF has no pages')
        for page in pdf.pages:
            if page.rotation or page.images or not page.chars or any(not c['upright'] for c in page.chars):
                raise ValueError('native path requires unrotated text-only pages; use OCR/review for mixed/image/empty pages')
            if any(c.get('text', '').startswith('(cid:') for c in page.chars):
                raise ValueError('unresolved font character mapping requires review')
            lines = page.extract_text_lines(layout=False, strip=False)
            if not lines:
                raise ValueError('no readable native lines')
            number = page.page_number
            pages.append(Page(number=number, width=page.width, height=page.height))
            for i, line in enumerate(lines, 1):
                source = Source(document_id=document_id, version=digest, page=number,
                    method='native', bbox=Box(x0=line['x0'], y0=line['top'], x1=line['x1'], y1=line['bottom']))
                blocks.append(TextBlock(block_id=f'p{number:04d}-native-{i:04d}', source=source, text=line['text']))
            png = io.BytesIO()
            page.to_image(resolution=100).original.save(png, format='PNG')
            destination = _store_raster(asset_dir, digest, f'page-{number:04d}.png', png.getvalue())
            source = Source(document_id=document_id, version=digest, page=number, method='raster',
                bbox=Box(x0=0, y0=0, x1=page.width, y1=page.height))
            blocks.append(ImageBlock(block_id=f'p{number:04d}-raster', source=source,
                asset_ref=str(destination), sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                caption=f'Original PDF page {number}; visual review required'))
    return FixtureInput(kind='pdf_ir', document=DocumentIR(document_id=document_id, version=digest,
        name=path.name, pages=pages, blocks=blocks, extraction=ExtractionInfo(engine='pdfplumber-native-v1',
            pages_processed=[p.number for p in pages], human_review_required=True,
            warnings=['Line order, headings, multi-column layouts, code indentation and table cells require explicit review.'])))


def main():
    parser = argparse.ArgumentParser(description='Read a native text PDF locally without model calls')
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.pdf.resolve() or (args.output.exists() and args.output.samefile(args.pdf)):
        parser.error('output must not overwrite the source PDF')
    result = extract_native_pdf(args.pdf, args.assets)
    if args.output.resolve() in {Path(b.asset_ref).resolve() for b in result.document.blocks if b.kind == 'image'}:
        parser.error('output must not overwrite source page rasters')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding='utf-8')
    print(f'Extracted {len(result.document.pages)} pages, {len(result.document.blocks)} blocks; review required')


if __name__ == '__main__':
    main()
