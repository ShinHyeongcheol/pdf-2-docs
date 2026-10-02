import argparse
import hashlib
from pathlib import Path

from .api import PIPELINE_VERSION, default_workflow
from .contracts import FixtureInput, Status
from .store import JobStore


def main():
    parser = argparse.ArgumentParser(description="Run authored, OCR or native PDF IR through the local document pipeline")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--db", type=Path, default=Path("output/jobs.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("output/run.json"))
    parser.add_argument("--request-key", default=None)
    args = parser.parse_args()
    source = FixtureInput.model_validate_json(args.fixture.read_text(encoding="utf-8"))
    store = JobStore(args.db)
    request_key = args.request_key or f"demo:{hashlib.sha256(source.model_dump_json().encode()).hexdigest()}:{PIPELINE_VERSION}"
    job = store.create(source, request_key, PIPELINE_VERSION)
    if job.status == Status.FAILED:
        job = store.resume(job.job_id)
    job = store.run(job.job_id, default_workflow(asset_root=args.fixture.resolve().parent if source.kind in {"ocr_ir", "pdf_ir"} else None))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(job.model_dump_json(indent=2), encoding="utf-8")
    print(f"job={job.job_id} status={job.status} revision={job.revision}")
    print(f"sections={len(job.outline.sections) if job.outline else 0} operations={len(job.publish_plan.operations) if job.publish_plan else 0}")
    print(f"output={args.output}")
    raise SystemExit(0 if job.status == Status.READY else 1)


if __name__ == "__main__":
    main()
