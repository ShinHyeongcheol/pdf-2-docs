"""Prepare/confirm a host-executed Notion MCP action. No credentials or live HTTP."""
import argparse
import json
from pathlib import Path

from .contracts import FixtureInput
from .notion_mcp import Checkpoint, confirm_publication, prepare_publication, save_checkpoint
from .notion_quiz import SectionPage
from .openai_adapter import ScriptedQuizAdapter
from .quiz import QuizResult, QuizWorkflow
from .review import HierarchicalOutline, ReviewLayer


def main():
    root = Path(__file__).resolve().parents[2] / "fixtures"
    parser = argparse.ArgumentParser(description="Prepare/confirm mock quiz publication through a connected host")
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--fetch-result", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=Path("output/notion-mcp-checkpoint.json"))
    parser.add_argument("--output", type=Path, default=Path("output/notion-mcp-prepared.json"))
    parser.add_argument("--source", type=Path, default=root/"synthetic.json")
    parser.add_argument("--outline", type=Path, default=root/"synthetic-outline.json")
    parser.add_argument("--review", type=Path, default=root/"synthetic-review.json")
    parser.add_argument("--quiz-result", type=Path)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    protected = [args.binding, args.fetch_result, args.source, args.outline, args.review, root/"synthetic-quiz.json"]
    if args.quiz_result: protected.append(args.quiz_result)
    def aliases(a, b):
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    if aliases(args.output, args.checkpoint) or any(aliases(out, path) for out in (args.output, args.checkpoint) for path in protected):
        parser.error("output/checkpoint must not overwrite inputs or each other")
    try:
        binding = SectionPage.model_validate_json(args.binding.read_text())
        packet = json.loads(args.fetch_result.read_text())
        checkpoint = Checkpoint.model_validate_json(args.checkpoint.read_text()) if args.checkpoint.exists() else Checkpoint(page_id=binding.page_id)
        if args.confirm:
            checkpoint = confirm_publication(packet, binding, checkpoint)
            save_checkpoint(args.checkpoint, checkpoint)
            data = {"status":"confirmed", "action":None, "checkpoint":checkpoint.model_dump(mode="json"), "provider_mode":"mock"}
        else:
            source = FixtureInput.model_validate_json(args.source.read_text())
            hierarchy = HierarchicalOutline.model_validate_json(args.outline.read_text())
            layer = ReviewLayer.model_validate_json(args.review.read_text())
            if args.quiz_result:
                result = QuizResult.model_validate_json(args.quiz_result.read_text())
            else:
                result = QuizWorkflow(ScriptedQuizAdapter([(root/"synthetic-quiz.json").read_text()])).run(source,hierarchy,layer,binding.section_id)
            prepared = prepare_publication(source,hierarchy,layer,result,binding,packet,checkpoint)
            # Persist before host dispatch; failed/unchanged results never expose a stale action.
            save_checkpoint(args.checkpoint, prepared.checkpoint)
            data = prepared.model_dump(mode="json")
    except Exception as exc:
        data = {"status":"failed_human_review", "action":None, "errors":["cli_error:"+type(exc).__name__], "provider_mode":"mock"}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
    print(f"mode=mock status={data['status']} action={'prepared' if data.get('action') else 'none'} host_dispatch=required")
    raise SystemExit(1 if data['status']=="failed_human_review" else 0)


if __name__ == "__main__":
    main()
