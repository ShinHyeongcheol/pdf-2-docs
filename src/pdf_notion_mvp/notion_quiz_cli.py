"""Authored synthetic demo only: no credentials, Notion transport or model calls."""
import argparse
from pathlib import Path

from .contracts import FixtureInput
from .notion_quiz import MemoryPageGateway, SectionPage, paragraph, plan_toggles, publish_mock
from .openai_adapter import ScriptedQuizAdapter
from .quiz import QuizWorkflow
from .review import HierarchicalOutline, ReviewLayer


def main():
    parser = argparse.ArgumentParser(description="Preview mock question/answer/explanation toggles")
    parser.add_argument("--output", type=Path, default=Path("output/mock-notion-quiz.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "fixtures"
    paths = [root / name for name in ("synthetic.json", "synthetic-outline.json", "synthetic-review.json", "synthetic-quiz.json")]
    if args.output.resolve() in {p.resolve() for p in paths} or (args.output.exists() and any(args.output.samefile(p) for p in paths)):
        parser.error("output must not overwrite fixture inputs")
    source = FixtureInput.model_validate_json(paths[0].read_text())
    hierarchy = HierarchicalOutline.model_validate_json(paths[1].read_text())
    layer = ReviewLayer.model_validate_json(paths[2].read_text())
    result = QuizWorkflow(ScriptedQuizAdapter([paths[3].read_text()])).run(source, hierarchy, layer, "unit.part")
    binding = SectionPage(hub_id="00000000-0000-4000-8000-000000000001", page_id="00000000-0000-4000-8000-000000000002",
        document_id=source.document.document_id, version=source.document.version, section_id=result.section_id)
    gateway = MemoryPageGateway(binding, [paragraph("직접 작성한 합성 사용자 메모")])
    inputs = (source, hierarchy, layer, result, binding)
    first = publish_mock(*inputs, gateway)
    second = publish_mock(*inputs, gateway)
    import json
    data = {"mode": "mock_only", "plan": plan_toggles(*inputs).model_dump(mode="json"),
        "first_run": first.model_dump(), "second_run": second.model_dump(), "page_children": gateway.children}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"mode=mock_only first={first.status} rerun={second.status} appends={gateway.append_calls}")


if __name__ == "__main__":
    main()
