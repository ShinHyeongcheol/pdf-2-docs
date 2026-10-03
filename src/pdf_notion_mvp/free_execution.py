"""Verified free-tier receipts, separated from the immutable paid reservation history."""
import hashlib
import os
import sqlite3
import subprocess
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field
from .contracts import Contract


class FreeTierEvidence(Contract):
    project_id: str = Field(pattern=r'^[a-z][a-z0-9-]{4,62}$')
    tier: Literal['Free'] = 'Free'
    model: Literal['gemini-3.1-flash-lite'] = 'gemini-3.1-flash-lite'
    verified_at: datetime
    expires_at: datetime
    image_paths: list[str] = Field(min_length=1,max_length=2)
    image_sha256: list[str] = Field(min_length=1,max_length=2)
    verification: Literal['visual_screenshots_and_runtime_project_match'] = 'visual_screenshots_and_runtime_project_match'
    project_matcher_path: str | None = None
    project_matcher_sha256: str | None = None


def regular(path):
    if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('regular absolute execution path required')
    if path.exists() and (not path.is_file() or path.stat().st_nlink!=1):
        raise ValueError('regular single-link execution file required')


def validate_evidence(evidence, now=None):
    evidence=FreeTierEvidence.model_validate(evidence.model_dump())
    now=now or datetime.now(timezone.utc)
    times=[evidence.verified_at,evidence.expires_at,now]
    if (any(t.tzinfo is None for t in times) or not evidence.verified_at<=now<evidence.expires_at
            or not timedelta(0)<evidence.expires_at-evidence.verified_at<=timedelta(days=1)):
        raise ValueError('free-tier evidence expired or invalid')
    if len(evidence.image_paths)!=len(evidence.image_sha256):
        raise ValueError('free-tier evidence files mismatch')
    for filename,digest in zip(evidence.image_paths,evidence.image_sha256):
        path=Path(filename);regular(path)
        if path.suffix!='.png' or not path.is_file() or not 0<path.stat().st_size<=2_000_000:
            raise ValueError('bounded screenshot evidence required')
        data=path.read_bytes()
        if not data.startswith(b'\x89PNG\r\n\x1a\n') or hashlib.sha256(data).hexdigest()!=digest:
            raise ValueError('free-tier screenshot changed')
    if bool(evidence.project_matcher_path)!=bool(evidence.project_matcher_sha256):
        raise ValueError('project matcher binding required')
    if evidence.project_matcher_path:
        matcher=Path(evidence.project_matcher_path);regular(matcher)
        if (not matcher.is_file() or not os.access(matcher,os.X_OK) or not 0<matcher.stat().st_size<=2_000_000
                or hashlib.sha256(matcher.read_bytes()).hexdigest()!=evidence.project_matcher_sha256):
            raise ValueError('approved project matcher changed')
    return evidence


def verified_project(key,evidence):
    """Only an explicitly approved local matcher receives the key, through stdin."""
    if not evidence.project_matcher_path:raise ValueError('runtime free project verifier required')
    result=subprocess.run([evidence.project_matcher_path,evidence.image_paths[-1]],input=key,
                          text=True,capture_output=True,timeout=15)
    if result.returncode or len(result.stdout)>4096:raise ValueError('free project verification failed')
    try:packet=json.loads(result.stdout)
    except ValueError:raise ValueError('free project verification failed') from None
    if (not isinstance(packet,dict) or packet.get('matched') is not True
            or packet.get('projects')!=[evidence.project_id]):
        raise ValueError('current key project differs from verified free project')
    return evidence.project_id


def paid_total_readonly(path):
    path=Path(path);regular(path)
    if path.suffix!='.sqlite' or not path.exists():raise ValueError('existing paid ledger required')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        if db.execute('SELECT value FROM ledger_identity').fetchall()!=[('pdf-notion-text-lesson-budget-v1',)]:
            raise ValueError('paid ledger identity mismatch')
        total=db.execute('SELECT coalesce(sum(cost),0) FROM reservations').fetchone()[0]
        if type(total) is not int or not 0<=total<=10_000_000:raise ValueError('paid budget exceeded')
        return total
    finally:db.close()


def free_connection(path):
    path=Path(path);regular(path)
    if path.suffix!='.sqlite' or any((p/'.git').exists() for p in [path.parent,*path.parents]):
        raise ValueError('private free ledger required')
    path.parent.mkdir(parents=True,exist_ok=True)
    new=False
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd);new=True
    except FileExistsError:pass
    regular(path)
    db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5,isolation_level=None)
    try:
        if new:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE ledger_identity(value TEXT NOT NULL)')
            db.execute("INSERT INTO ledger_identity VALUES('pdf-notion-verified-free-v1')")
            db.execute('CREATE TABLE reservations(operation TEXT PRIMARY KEY,run_id TEXT UNIQUE NOT NULL,cost INTEGER NOT NULL CHECK(cost=0),status TEXT NOT NULL,result_path TEXT,result_sha TEXT,request_count INTEGER NOT NULL DEFAULT 0,error_code TEXT)')
            db.execute('COMMIT')
        elif db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()!=[('ledger_identity',),('reservations',)]:
            raise ValueError('existing file is not a free execution ledger')
        if db.execute('SELECT value FROM ledger_identity').fetchall()!=[('pdf-notion-verified-free-v1',)]:
            raise ValueError('free ledger identity mismatch')
        return db
    except BaseException:
        if db.in_transaction:db.execute('ROLLBACK')
        db.close();raise
