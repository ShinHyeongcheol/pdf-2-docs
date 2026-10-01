"""Authored local RAG demo/evaluation; no SDK, keys or external transport."""
import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract, FixtureInput
from .live_run import artifact_path
from .notion_quiz import SectionPage
from .quiz import GenerationBlocked
from .rag import MockAnswerAdapter, RagWorkflow, extractive_draft, prepare_index
from .review import HierarchicalOutline, ReviewLayer, document_digest


class RagFixture(Contract):
    source: FixtureInput
    hierarchy: HierarchicalOutline
    review: ReviewLayer
    bindings: list[SectionPage]


class EvalCase(Contract):
    case_id: str
    kind: Literal["answerable", "no_evidence", "wrong_quote", "candidate", "reindex", "source_mix"]
    question: str
    expected_status: Literal["ready_for_review", "unknown", "failed_human_review"]
    expected_block_ids: list[str]


class EvalSet(Contract):
    cases: list[EvalCase] = Field(min_length=1,max_length=20)


def load_fixture(root: Path):
    path = root/"fixtures/synthetic-rag.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size>100_000:
        raise ValueError("bounded authored RAG fixture required")
    return RagFixture.model_validate_json(path.read_text())


def revised_fixture(fixture):
    updated = fixture.model_copy(deep=True)
    version = "authored-v2"
    updated.source.document.version = version
    for block in updated.source.document.blocks: block.source.version = version
    updated.source.document.blocks[1].text = "재개 기능은 새로운 복구지점부터 시작합니다."
    updated.hierarchy.version = updated.review.version = version
    for correction in updated.review.corrections: correction.source.version = version
    updated.review.source_digest = document_digest(updated.source.document)
    for binding in updated.bindings: binding.version = version
    return updated


def run_fixture(fixture, question, generator=None, *, index=None):
    return RagWorkflow(generator).run(fixture.source,fixture.hierarchy,fixture.review,question,fixture.bindings,index=index)


def evaluate(fixture, cases):
    rows=[]
    for case in cases:
        current=fixture
        stale_rejected=None
        generator=MockAnswerAdapter()
        if case.kind == "reindex":
            old=prepare_index(fixture.source,fixture.hierarchy,fixture.review,fixture.bindings)
            current=revised_fixture(fixture)
            try: run_fixture(current,case.question,index=old)
            except ValueError: stale_rejected=True
            else: stale_rejected=False
        if case.kind in {"wrong_quote","source_mix"}:
            class Attacking(MockAnswerAdapter):
                def respond(self,context):
                    self.calls+=1
                    draft=extractive_draft(context)
                    if case.kind == "wrong_quote": draft.citations[0].quote="근거에 없는 합성 오인용"
                    else: draft.citations[0].source.document_id="other-synthetic-document"
                    draft.answer="근거 원문:\n"+"\n".join(c.quote for c in draft.citations)
                    return draft
            generator=Attacking()
        result=run_fixture(current,case.question,generator)
        ids=[c.source_block_id for c in result.draft.citations] if result.draft else []
        rows.append({"case_id":case.case_id,"kind":case.kind,"status":result.status,
            "source_block_ids":ids,"stale_index_rejected":stale_rejected,"mock_calls":generator.calls,
            "passed":result.status==case.expected_status and ids==case.expected_block_ids and stale_rejected is not False})
    return {"passed":all(r["passed"] for r in rows),"cases":rows}


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
    parser=argparse.ArgumentParser(description="Ask an authored synthetic local lexical RAG fixture; no network")
    parser.add_argument("--question",default="재개 체크포인트")
    parser.add_argument("--eval",action="store_true")
    parser.add_argument("--output",type=Path,default=Path("output/mock-rag.json"))
    args=parser.parse_args(argv)
    root=Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    try:
        output=artifact_path(root,args.output)
        fixture=load_fixture(root)
        data={"mode":"lexical_local_mock_answer","actual_embeddings_used":False,"vector_search_implemented":False,
              "actual_key_reads":0,"actual_model_requests":0,"notion_requests":0}
        if args.eval:
            path=root/"fixtures/synthetic-rag-eval.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size>100_000: raise ValueError("bounded authored evaluation required")
            evaluation=evaluate(fixture,EvalSet.model_validate_json(path.read_text()).cases)
            data.update(status="eval_passed" if evaluation["passed"] else "eval_failed",evaluation=evaluation)
        else:
            generator=MockAnswerAdapter()
            result=run_fixture(fixture,args.question,generator)
            data.update(status=result.status,result=result.model_dump(mode="json"),mock_calls=generator.calls)
        _save_json(output,data)
        print(f"mode=lexical_local_mock_answer status={data['status']} embeddings=disabled model_network=disabled")
        raise SystemExit(1 if data["status"] in {"eval_failed","failed_human_review"} else 0)
    except (GenerationBlocked,ValueError,TypeError,OSError) as exc:
        print("rag_error:"+type(exc).__name__)
        raise SystemExit(1) from None


if __name__=="__main__": main()
