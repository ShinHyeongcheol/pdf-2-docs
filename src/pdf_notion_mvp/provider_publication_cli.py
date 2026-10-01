"""Offline provider-result review plans and persistent mock Notion page rehearsal."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from .live_run import RunApproval, RunCompletion, artifact_path
from .notion_quiz import MemoryPageGateway, PageSnapshot, SectionPage, paragraph
from .provider_publication import plan_provider_toggles, publish_provider_mock
from .quiz import GenerationBlocked


def _save_mock_page(path: Path, snapshot: PageSnapshot) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent,
                prefix="."+path.name+"-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(snapshot.model_dump_json(indent=2)+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None, *, project_root=None):
    parser = argparse.ArgumentParser(description="Revalidate a completed provider run into a section review plan; no model/Notion network")
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("output/provider-notion-plan.json"))
    parser.add_argument("--mock-publish", action="store_true")
    parser.add_argument("--mock-page", type=Path, default=Path("output/mock-provider-page.json"))
    args = parser.parse_args(argv)
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        approval_path, result_path, binding_path, output, page = [artifact_path(root, path) for path in
            (args.approval, args.result, args.binding, args.output, args.mock_page)]
        def aliases(a, b):
            return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
        if aliases(output, page) or any(aliases(out, item) for out in (output, page) for item in (approval_path, result_path, binding_path)):
            raise ValueError("output/page must not overwrite inputs or each other")
        approval = RunApproval.model_validate_json(approval_path.read_text())
        completion = RunCompletion.model_validate_json(result_path.read_text())
        binding = SectionPage.model_validate_json(binding_path.read_text())
        plan = plan_provider_toggles(root, approval, completion, binding)
        data = {"mode":"provider_review_only", "status":"plan_prepared", "model_network":False,
            "notion_network":False, "plan":plan.model_dump(mode="json"), "receipt":None}
        if args.mock_publish:
            if page.exists():
                snapshot = PageSnapshot.model_validate_json(page.read_text())
                if snapshot.binding != binding or not snapshot.complete:
                    raise ValueError("incomplete or mismatched mock page")
                gateway = MemoryPageGateway(binding, snapshot.children)
            else:
                gateway = MemoryPageGateway(binding, [paragraph("직접 작성한 합성 사용자 메모")])
            receipt = publish_provider_mock(root, approval, completion, binding, gateway)
            data.update(status=receipt.status, receipt=receipt.model_dump(mode="json"))
            if receipt.status != "failed_human_review":
                page.parent.mkdir(parents=True, exist_ok=True)
                _save_mock_page(page, gateway.snapshot(binding))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
        print(f"mode=provider_review_only status={data['status']} model_network=disabled notion_network=disabled")
        raise SystemExit(1 if data["status"] == "failed_human_review" else 0)
    except (GenerationBlocked, OSError, ValueError, TypeError) as exc:
        print("status=failed_human_review error="+type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
