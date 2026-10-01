import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .contracts import FixtureInput, Job, Status
from .workflow import Workflow, transition


class ConflictError(ValueError):
    pass


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    fingerprint TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_keys (
                    request_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id)
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, source: FixtureInput, request_key: str, pipeline_version: str) -> Job:
        source = FixtureInput.model_validate(source.model_dump())
        canonical = json.dumps({"input": source.model_dump(), "pipeline_version": pipeline_version}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT fingerprint, job_id FROM request_keys WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ConflictError("request key already belongs to different input/version")
                return self.read(conn, existing[1])
            row = conn.execute("SELECT payload FROM jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row:
                job = Job.model_validate_json(row[0])
            else:
                job = Job(job_id=str(uuid4()), fingerprint=fingerprint, pipeline_version=pipeline_version, input=source)
                conn.execute("INSERT INTO jobs VALUES (?, ?, ?)", (job.job_id, fingerprint, job.model_dump_json()))
            conn.execute("INSERT INTO request_keys VALUES (?, ?, ?)", (request_key, fingerprint, job.job_id))
            return job

    @staticmethod
    def read(conn: sqlite3.Connection, job_id: str) -> Job:
        row = conn.execute("SELECT payload FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return Job.model_validate_json(row[0])

    def get(self, job_id: str) -> Job:
        with self.connection() as conn:
            return self.read(conn, job_id)

    def advance(self, job_id: str, workflow: Workflow, expected_revision: int | None = None) -> Job:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self.read(conn, job_id)
            if job.pipeline_version != workflow.pipeline_version:
                raise ConflictError("workflow version does not match stored job")
            if expected_revision is not None and job.revision != expected_revision:
                raise ConflictError("stale revision")
            next_job = Job.model_validate(workflow.advance(job).model_dump())
            conn.execute("UPDATE jobs SET payload=? WHERE job_id=?", (next_job.model_dump_json(), job_id))
            return next_job

    def resume(self, job_id: str, expected_revision: int | None = None) -> Job:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self.read(conn, job_id)
            if expected_revision is not None and job.revision != expected_revision:
                raise ConflictError("stale revision")
            if job.status != Status.FAILED:
                raise ConflictError("only failed jobs can resume")
            job = transition(job, job.last_success, resume=True)
            conn.execute("UPDATE jobs SET payload=? WHERE job_id=?", (job.model_dump_json(), job_id))
            return job

    def run(self, job_id: str, workflow: Workflow) -> Job:
        while True:
            job = self.advance(job_id, workflow)
            if job.status in {Status.READY, Status.REJECTED, Status.FAILED}:
                return job
