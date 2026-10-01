"""Offline-only authored mock demo. This entry point cannot enable OpenAI calls."""
import argparse
from pathlib import Path

from .contracts import FixtureInput
from .openai_adapter import ScriptedQuizAdapter
from .quiz import QuizWorkflow
from .review import HierarchicalOutline, ReviewLayer


def main():
    parser = argparse.ArgumentParser(description="Run an authored mock quiz through generation and verification")
    parser.add_argument("--output",type=Path,default=Path("output/mock-quiz.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "fixtures"
    inputs = [root/n for n in ("synthetic.json","synthetic-outline.json","synthetic-review.json","synthetic-quiz.json")]
    if args.output.resolve() in {p.resolve() for p in inputs} or (args.output.exists() and any(args.output.samefile(p) for p in inputs)):
        parser.error("output must not overwrite fixture inputs")
    source = FixtureInput.model_validate_json(inputs[0].read_text())
    hierarchy = HierarchicalOutline.model_validate_json(inputs[1].read_text())
    layer = ReviewLayer.model_validate_json(inputs[2].read_text())
    result = QuizWorkflow(ScriptedQuizAdapter([inputs[3].read_text()])).run(source,hierarchy,layer,"unit.part")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2))
    print(f"mode={result.provider_mode} status={result.status} attempts={result.attempts}")
    raise SystemExit(0 if result.status == "ready_for_review" else 1)


if __name__ == "__main__":
    main()
