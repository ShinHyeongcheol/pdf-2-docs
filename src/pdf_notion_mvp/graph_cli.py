"""Authored graph demo only; no SDK, credentials, model or Notion transport."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from .contracts import Contract, FixtureInput
from .graph_review import (
    GraphDraft, GraphEvidence, ScriptedGraphAdapter, generate_graph,
    plan_graph_toggle, publish_graph_mock,
)
from .live_run import artifact_path
from .notion_quiz import MemoryPageGateway, PageSnapshot, SectionPage, paragraph
from .quiz import GenerationBlocked
from .review import HierarchicalOutline, ReviewLayer


class GraphFixture(Contract):
    source: FixtureInput
    hierarchy: HierarchicalOutline
    review: ReviewLayer
    evidence: GraphEvidence
    mock_draft: GraphDraft
    binding: SectionPage


def load_graph_fixture(root: Path):
    files = [root / "fixtures" / n for n in ("synthetic-graph.json", "synthetic-graph.svg")]
    if any(p.is_symlink() or not p.is_file() or p.stat().st_size > 100_000 for p in files):
        raise ValueError("bounded authored graph fixture files required")
    return GraphFixture.model_validate_json(files[0].read_text()), files[1].read_bytes()


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix="."+path.name+"-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None, *, project_root=None):
    parser = argparse.ArgumentParser(description="Prepare authored mock graph review toggles without network")
    parser.add_argument("--output", type=Path, default=Path("output/mock-graph-plan.json"))
    parser.add_argument("--mock-publish", action="store_true")
    parser.add_argument("--mock-page", type=Path, default=Path("output/mock-graph-page.json"))
    args = parser.parse_args(argv)
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    try:
        output = artifact_path(root, args.output)
        page = artifact_path(root, args.mock_page)
        if output.resolve() == page.resolve():
            raise ValueError("plan and mock page paths must differ")
        fixture, image_bytes = load_graph_fixture(root)
        adapter = ScriptedGraphAdapter(fixture.mock_draft)
        inputs = (fixture.source, fixture.hierarchy, fixture.review, fixture.evidence, image_bytes)
        result = generate_graph(*inputs, adapter)
        if result.status != "ready_for_review":
            raise ValueError("authored graph result did not pass independent verification")
        plan = plan_graph_toggle(*inputs, result, fixture.binding)
        data = {"mode": "mock_graph_review_only", "status": "plan_ready", "result": result.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"), "mock_adapter_calls": adapter.calls,
            "actual_key_reads": 0, "actual_model_requests": 0, "notion_requests": 0}
        if args.mock_publish:
            if page.exists():
                saved = PageSnapshot.model_validate_json(page.read_text())
                if saved.binding != fixture.binding or not saved.complete:
                    raise ValueError("mock graph page binding or completeness mismatch")
                children = saved.children
            else:
                children = [paragraph("직접 작성한 합성 사용자 메모")]
            gateway = MemoryPageGateway(fixture.binding, children)
            receipt = publish_graph_mock(*inputs, result, fixture.binding, gateway)
            data.update(status=receipt.status, receipt=receipt.model_dump(mode="json"))
            if receipt.status != "failed_human_review":
                _write_json_atomic(page, gateway.snapshot(fixture.binding).model_dump(mode="json"))
        _write_json_atomic(output, data)
        print(f"mode=mock_graph_review_only status={data['status']} model_network=disabled notion_network=disabled")
        raise SystemExit(1 if data["status"] == "failed_human_review" else 0)
    except (GenerationBlocked, ValueError, TypeError, OSError) as exc:
        print("graph_error:"+type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
