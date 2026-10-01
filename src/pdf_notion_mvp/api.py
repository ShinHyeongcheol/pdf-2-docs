import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field

from .adapters import DryRunNotionPlanner, FixtureExtractor, LocalKnowledgeAdapter
from .contracts import Contract, FixtureInput, Job
from .store import ConflictError, JobStore
from .workflow import PIPELINE_VERSION, Workflow


class CreateJob(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    input: FixtureInput


class Mutation(Contract):
    expected_revision: int = Field(ge=0)


def default_workflow(asset_root: Path | None = None) -> Workflow:
    return Workflow(FixtureExtractor(), LocalKnowledgeAdapter(), DryRunNotionPlanner(), asset_root=asset_root)


def create_app(db_path: Path | None = None, workflow: Workflow | None = None) -> FastAPI:
    store = JobStore(db_path or Path(os.environ.get("PDF_NOTION_DB", "output/jobs.sqlite")))
    asset_root = os.environ.get("PDF_NOTION_ASSET_ROOT")
    workflow = workflow or default_workflow(Path(asset_root) if asset_root else None)
    app = FastAPI(title="PDF to Notion local MVP", version="0.1.0")

    @app.exception_handler(ConflictError)
    async def conflict(_request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse(status_code=404, content={"detail": "job not found"})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(_request, _exc):
        return JSONResponse(status_code=503, content={"detail": "local store unavailable; retry later"})

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "offline_dry_run", "pipeline_version": PIPELINE_VERSION}

    @app.post("/jobs", response_model=Job)
    def create_job(request: CreateJob):
        return store.create(request.input, request.request_key, PIPELINE_VERSION)

    @app.get("/jobs/{job_id}", response_model=Job)
    def get_job(job_id: str):
        return store.get(job_id)

    @app.post("/jobs/{job_id}/advance", response_model=Job)
    def advance(job_id: str, request: Mutation):
        if store.get(job_id).pipeline_version != PIPELINE_VERSION:
            raise ConflictError("job belongs to a different pipeline version; create a new job")
        return store.advance(job_id, workflow, request.expected_revision)

    @app.post("/jobs/{job_id}/resume", response_model=Job)
    def resume(job_id: str, request: Mutation):
        if store.get(job_id).pipeline_version != PIPELINE_VERSION:
            raise ConflictError("job belongs to a different pipeline version; create a new job")
        return store.resume(job_id, request.expected_revision)

    @app.get("/jobs/{job_id}/publish-plan")
    def publish_plan(job_id: str):
        job = store.get(job_id)
        if job.publish_plan is None:
            raise HTTPException(status_code=409, detail="no verified publishing plan")
        return job.publish_plan

    return app
