"""Explicit one-section local cloze review plans; no model or Notion calls."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from .live_run import artifact_path
from .local_quiz import plan_local_quiz
from .quiz import GenerationBlocked
from .rag_files import load_review_files


def _save_json(path,value):
    temporary=None
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,prefix="."+path.name+"-",delete=False) as stream:
            temporary=Path(stream.name)
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def main(argv=None, *, project_root=None):
    parser=argparse.ArgumentParser(description="Create deterministic local cloze review questions from one confirmed section; no network")
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--outline",type=Path,required=True)
    parser.add_argument("--review",type=Path,required=True)
    parser.add_argument("--section",required=True)
    parser.add_argument("--max-questions",type=int,choices=[1,2,3],default=1)
    parser.add_argument("--output",type=Path,default=Path("output/local-rule-quiz.json"))
    args=parser.parse_args(argv)
    root=Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    try:
        output=artifact_path(root,args.output)
        files=load_review_files(args.source,args.outline,args.review,[],output)
        plan=plan_local_quiz(files.source,files.hierarchy,files.review,args.section,
            asset_root=files.asset_root,max_questions=args.max_questions)
        data={"mode":"local_rule_review_only","plan":plan.model_dump(mode="json"),
              "actual_key_reads":0,"actual_model_requests":0,"notion_requests":0}
        if len(json.dumps(data,ensure_ascii=False).encode())>100_000:
            raise ValueError("local question plan exceeds byte limit")
        _save_json(output,data)
        print(f"generation=local_rule status={plan.status} questions={len(plan.questions)} model_network=disabled notion_network=disabled")
        raise SystemExit(0 if plan.status=="ready_for_review" else 1)
    except (GenerationBlocked,ValueError,TypeError,OSError) as exc:
        print("local_quiz_error:"+type(exc).__name__)
        raise SystemExit(1) from None


if __name__=="__main__": main()
