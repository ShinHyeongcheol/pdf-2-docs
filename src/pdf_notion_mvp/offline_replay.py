"""Credential-free replay of reviewed cache through durable mock publication/readback."""
import os
import sqlite3
import fcntl
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5
from typing import Annotated

from pydantic import Field

from .contracts import Contract
from .document_audit import audit_document, plan_batches
from .document_study import accept_batch, render_batch
from .lesson_generation import _regular
from .local_run import LocalRunStore
from .pdf_inspect import inspect_pdf
from .rag_files import load_review_files, read_json_input
from .study_bundle import encode, private_destination, sha

VERSION = 'offline-reviewed-replay-v1'
SOURCE_URL = 'https://app.notion.com/p/00000000000000000000000000000001'


class CachedBatch(Contract):
    result_path: Path
    review_path: Path
    revision_path: Path | None = None


class ReusedReading(Contract):
    root: Path
    review_path: Path
    module_key: str
    pages: list[Annotated[int, Field(strict=True, ge=1)]] = Field(min_length=1)


class ReplaySpec(Contract):
    pdf_path: Path
    source_path: Path
    outline_path: Path
    review_path: Path
    budget_path: Path
    batches: list[CachedBatch] = Field(min_length=1)
    reused_reading: list[ReusedReading] = Field(default_factory=list)
    budget_cap_micro_usd: int = Field(default=10_000_000, ge=0, strict=True)


def file_digest(path):
    path = Path(path); _regular(path)
    return sha(path.read_bytes())


def budget_snapshot(path):
    path = Path(path).resolve(); file_digest(path)
    with sqlite3.connect('file:'+quote(str(path), safe='/')+'?mode=ro', uri=True) as db:
        if db.execute('SELECT value FROM ledger_identity').fetchall() != [('pdf-notion-text-lesson-budget-v1',)]:
            raise ValueError('unknown shared budget ledger')
        if db.execute('SELECT count(*) FROM reservations WHERE cost<0 OR request_count<0 OR request_count>1').fetchone()[0]:
            raise ValueError('invalid original budget receipts')
        count, cost, requests = db.execute('SELECT count(*),coalesce(sum(cost),0),coalesce(sum(request_count),0) FROM reservations').fetchone()
    return dict(reservations=count, reserved_micro_usd=cost, requests=requests, sha256=file_digest(path))


def reading_reuse(item, version):
    """Reuse an independently hash-bound reading artifact, without promoting its OCR."""
    root = item.root.resolve()
    if item.root.is_symlink() or any(p.is_symlink() for p in item.root.parents):
        raise ValueError('regular reading root required')
    review = read_json_input(item.review_path, max_bytes=2_000_000)
    bindings = review.get('bound_reading_sha256', {})
    if review.get('decision') != 'accepted' or not bindings:
        raise ValueError('accepted hash-bound reading review required')
    entries = list(root.rglob('*'))
    if any(p.is_symlink() for p in entries) or {p.relative_to(root).as_posix() for p in entries if p.is_file()} != set(bindings):
        raise ValueError('reading artifact file set changed')
    for name, expected in bindings.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('relative artifact binding required')
        if file_digest(root/relative) != expected:
            raise ValueError('reading artifact changed after review')
    if 'manifest.json' not in bindings or 'original.pdf' not in bindings:
        raise ValueError('source-bound reading manifest required')
    manifest = read_json_input(root/'manifest.json', max_bytes=2_000_000)
    if manifest['source_pdf_sha256'] != version or bindings['original.pdf'] != version:
        raise ValueError('reading document version mismatch')
    selected = [m for m in manifest['modules'] if m['key'] == item.module_key]
    if len(selected) != 1 or selected[0]['file'] not in bindings:
        raise ValueError('one reviewed module required')
    pages = item.pages
    if (any(type(p) is not int or p < 1 for p in pages) or len(set(pages)) != len(pages) or
            not set(pages) <= set(selected[0]['source_pages'])):
        raise ValueError('reused page scope mismatch')
    asset = root/selected[0]['file']
    if asset.suffix != '.html' or asset.stat().st_size > 2_000_000:
        raise ValueError('bounded reviewed HTML reading asset required')
    return dict(module=item.module_key, title=selected[0]['title'], pages=pages, content=asset.read_text(),
                representation='reviewed_html_artifact', artifact_sha256=bindings[selected[0]['file']],
                review_sha256=file_digest(item.review_path), semantics_newly_confirmed=False)


class MockCreateResult(Contract):
    page_id: str = Field(pattern=r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
    created: bool = Field(strict=True)


class SQLiteMockGateway:
    """Durable test service. Its owner-key lookup is NOT an implemented Notion API."""
    mode = 'durable_mock'

    def __init__(self, path):
        self.path = Path(path); private_destination(self.path.parent, [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize first-file creation as well as DDL. Existing unknown files
        # are never initialized or overwritten after a crashed bootstrap.
        lock_path = self.path.with_suffix(self.path.suffix+'.bootstrap.lock')
        lock_fd = os.open(lock_path, os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        try:
            _regular(lock_path)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if (os.fstat(lock_fd).st_dev, os.fstat(lock_fd).st_ino) != (lock_path.stat().st_dev, lock_path.stat().st_ino):
                raise ValueError('mock bootstrap lock changed')
            new = False
            try:
                fd = os.open(self.path, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); os.close(fd); new = True
            except FileExistsError: pass
            _regular(self.path)
            self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)
            self.initialized = False
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if new:
                    db.execute('CREATE TABLE identity(value TEXT PRIMARY KEY)')
                    db.execute('INSERT INTO identity VALUES(?)', ('offline-replay-mock-v1',))
                    db.execute('CREATE TABLE pages(owner TEXT PRIMARY KEY,id TEXT UNIQUE,title TEXT,content TEXT,memo TEXT)')
                if db.execute('SELECT value FROM identity').fetchall() != [('offline-replay-mock-v1',)]:
                    raise ValueError('unknown mock gateway database')
            self.initialized = True
        finally:
            os.close(lock_fd)

    @contextmanager
    def connection(self):
        private_destination(self.path.parent, []); _regular(self.path)
        if (self.path.stat().st_dev, self.path.stat().st_ino) != self.identity:
            raise ValueError('mock gateway file changed')
        db = sqlite3.connect(self.path.absolute().as_uri()+'?mode=rw', uri=True, timeout=10)
        try:
            if (self.path.stat().st_dev, self.path.stat().st_ino) != self.identity:
                raise ValueError('mock gateway file changed while opening')
            if self.initialized and db.execute('SELECT value FROM identity').fetchall() != [('offline-replay-mock-v1',)]:
                raise ValueError('mock gateway identity changed')
            with db:
                yield db
        finally:
            db.close()

    def find(self, owner):
        with self.connection() as db:
            return [r[0] for r in db.execute('SELECT id FROM pages WHERE owner=?', (owner,))]

    def create(self, owner, title, content):
        identifier = str(uuid5(NAMESPACE_URL, VERSION+':'+owner))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,title,content FROM pages WHERE owner=?',(owner,)).fetchall()
            if rows:
                if rows != [(identifier,title,content)]:
                    raise ValueError('existing mock owner content differs')
                created = False
            else:
                db.execute('INSERT INTO pages VALUES(?,?,?,?,?)', (owner,identifier,title,content,''))
                created = True
        return MockCreateResult(page_id=identifier,created=created)

    def fetch(self, identifier):
        with self.connection() as db:
            rows = db.execute('SELECT owner,id,title,content,memo FROM pages WHERE id=?', (identifier,)).fetchall()
        if len(rows) != 1: raise ValueError('one complete mock page required')
        owner, identifier, title, content, memo = rows[0]
        return dict(mode=self.mode, owner=owner, id=identifier, title=title, content=content, user_memo=memo)


def run_replay(spec, *, output_dir, gateway=None, hook=None):
    """Always revalidate current inputs and fetch stored content, even after completion."""
    spec = ReplaySpec.model_validate(spec.model_dump())
    input_paths = [spec.pdf_path,spec.source_path,spec.outline_path,spec.review_path,spec.budget_path]
    for batch in spec.batches:
        input_paths += [batch.result_path,batch.review_path]+([batch.revision_path] if batch.revision_path else [])
    for reused in spec.reused_reading:
        review = read_json_input(reused.review_path,max_bytes=2_000_000)
        bindings = review.get('bound_reading_sha256', {})
        for name in bindings:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('relative artifact binding required')
            input_paths.append(reused.root/relative)
        input_paths += [reused.review_path,reused.root/'manifest.json']
    root = private_destination(Path(output_dir), input_paths)
    before = {str(p.resolve()):file_digest(p) for p in input_paths}
    budget = budget_snapshot(spec.budget_path)
    if budget['reserved_micro_usd'] > spec.budget_cap_micro_usd:
        raise ValueError('past reservations exceed stated replay budget')
    files = load_review_files(spec.source_path,spec.outline_path,spec.review_path,[],root/'unused.json')
    inspection = inspect_pdf(spec.pdf_path)
    audit = audit_document(files,pdf_path=spec.pdf_path)
    if inspection.sha256 != audit['version'] or inspection.page_count != audit['page_count']:
        raise ValueError('PDF/extracted cache page or version mismatch')
    if files.asset_root is not None:
        for block in files.source.document.blocks:
            if block.kind == 'image':
                path = Path(block.asset_ref)
                if not path.is_absolute(): path = files.asset_root/path
                digest = file_digest(path)
                if digest != block.sha256: raise ValueError('source raster changed')
                input_paths.append(path);before[str(path.resolve())] = digest
    def require_current():
        current = {str(p.resolve()):file_digest(p) for p in input_paths}
        if current != before or budget_snapshot(spec.budget_path) != budget:
            raise ValueError('inputs or API ledger changed during replay')
        for item in spec.reused_reading: reading_reuse(item,audit['version'])
    require_current()
    run_key = sha(encode([VERSION,spec.model_dump(mode='json'),before]))
    journal = LocalRunStore(root/'replay.sqlite')
    def stage(number, value):
        saved = journal.process(run_key,[number],lambda _:value)[0]
        if saved != value: raise ValueError('saved replay stage changed')
        if hook: hook(number)
        require_current()
    stage(1,dict(stage='source_verified',pdf_sha256=inspection.sha256,pages=inspection.page_count,
                 audit_digest=audit['audit_digest'],extraction_cache_kind=files.source.kind,ocr_cache_reused=files.source.kind=='ocr_ir',ocr_semantics_verified=False))
    materials = []
    expected_paths = [spec.source_path.resolve(),spec.outline_path.resolve(),spec.review_path.resolve()]
    for batch in spec.batches:
        raw = read_json_input(batch.result_path,max_bytes=2_000_000)
        if [Path(p).resolve() for p in raw['input_paths']] != expected_paths:
            raise ValueError('cached generation uses different replay inputs')
        materials.append(accept_batch(batch.result_path,batch.review_path,spec.budget_path,revision_path=batch.revision_path))
    reused = [reading_reuse(i,audit['version']) for i in spec.reused_reading]
    pages = [p for m in materials for p in m['context']['pages']]+[p for item in reused for p in item['pages']]
    if sorted(pages) != list(range(1,audit['page_count']+1)):
        raise ValueError('reused material must cover every PDF page exactly once')
    stage(2,dict(stage='reviewed_cache_reused',material_digests=[m['accepted_material_digest'] for m in materials],
                 reviewed_reading=[{k:v for k,v in item.items() if k!='content'} for item in reused],pages=sorted(pages),additional_model_calls=0))
    # This is replay of approved batch artifacts, not a rewrite of the teaching units.
    regeneration = plan_batches(audit,reuse_pages=pages)
    if regeneration['batches']: raise ValueError('replay must never regenerate')
    plans = []
    for material in materials:
        content = render_batch(material,SOURCE_URL)
        owner = sha(encode([VERSION,audit['version'],material['accepted_material_digest'],content]))
        plans.append(dict(representation='reviewed_native_batch',owner=owner,title='Reviewed cache · p'+str(material['context']['pages'][0]),
                          content=content,content_sha256=sha(content.encode()),pages=material['context']['pages']))
    for item in reused:
        content = item['content']
        owner = sha(encode([VERSION,audit['version'],item['artifact_sha256'],item['pages'],content]))
        plans.append(dict(representation=item['representation'],owner=owner,title=item['title'],content=content,
                          content_sha256=sha(content.encode()),pages=item['pages']))
    stage(3,dict(stage='mock_publish_plan_ready',operations=[{k:v for k,v in p.items() if k!='content'} for p in plans],
                 new_generation_batches=0))
    gateway = gateway or SQLiteMockGateway(root/'mock-pages.sqlite')
    if getattr(gateway,'mode',None) != 'durable_mock':
        raise ValueError('only explicitly mock publication is allowed')
    confirmed = []; creates = 0
    for plan in plans:
        require_current()
        existing = gateway.find(plan['owner'])
        if len(existing) > 1: raise ValueError('ambiguous mock owner; do not create')
        if existing: identifier = existing[0]
        else:
            require_current()
            # A timeout may happen after the mock commits; restart uses find, never blind retry.
            creation = gateway.create(plan['owner'],plan['title'],plan['content'])
            creation = MockCreateResult.model_validate(creation.model_dump())
            identifier = creation.page_id; creates += int(creation.created)
        require_current()
        record = gateway.fetch(identifier)
        if (record.get('mode'),record.get('id'),record.get('owner'),record.get('title'),record.get('content')) != (
                'durable_mock',identifier,plan['owner'],plan['title'],plan['content']):
            raise ValueError('stored readback changed; preserve page and user memo')
        require_current()
        confirmed.append(dict(owner=plan['owner'],id=identifier,representation=plan['representation'],pages=plan['pages'],content_sha256=plan['content_sha256']))
    stage(4,dict(stage='stored_mock_readback_verified',pages=confirmed))
    require_current()
    result = dict(status='completed_mock_replay',run_key=run_key,stages=['source_verified','reviewed_cache_reused',
        'mock_publish_plan_ready','stored_mock_readback_verified'],source_pages=audit['page_count'],
        source_blocks=audit['block_count'],generated_batches_reused=len(materials),reading_pages_reused=sum(len(i['pages']) for i in reused),
        mock_pages_verified=len(confirmed),mock_source_pages_verified=sorted(p for record in confirmed for p in record['pages']),
        publication_representations=[record['representation'] for record in confirmed],mock_creates_this_invocation=creates,actual_notion_calls=0,
        actual_model_calls=0,key_reads=0,embedding_calls=0,budget_before=budget,budget_after=budget,
        actual_pdf_inspected=True,existing_ocr_cache_reused=files.source.kind=='ocr_ir',ocr_semantics_newly_verified=False,
        actual_notion_readback_verified=False,real_provider_generation_repeated=False)
    return result
