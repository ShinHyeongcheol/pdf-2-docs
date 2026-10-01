"""Mock by default. Plan is offline; live requires a single reviewed approval file."""
import argparse
import json
from decimal import Decimal
from pathlib import Path

from .live_run import RunApproval, artifact_path, create_proposal, execute_approved, load_inputs
from .openai_adapter import ScriptedQuizAdapter
from .quiz import GenerationBlocked, QuizPolicy, QuizWorkflow


def main(argv=None, *, project_root=None, client_factory=None, key_provider=None, now=None):
    parser = argparse.ArgumentParser(description="One synthetic quiz: mock, offline approval plan, or explicitly approved Gemini run")
    parser.add_argument("--mode", choices=["mock", "plan", "live"], default="mock")
    parser.add_argument("--model")
    parser.add_argument("--budget-usd", type=Decimal)
    parser.add_argument("--max-request-bytes", type=int, default=12_000)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        if args.mode == "live":
            if not args.approval or not args.expected_plan_sha256 or args.model or args.budget_usd is not None:
                raise GenerationBlocked("live requires only an approved file and explicit plan fingerprint")
            if (args.max_request_bytes, args.max_output_tokens, args.timeout_seconds) != (12_000, 512, 20):
                raise GenerationBlocked("live limit overrides are unsupported; approve a new plan")
            approval_path = artifact_path(root, args.approval)
            approval = RunApproval.model_validate_json(approval_path.read_text())
            output = artifact_path(root, args.output or Path("output")/(str(approval.spec.run_id)+"-result.json"))
            if output.exists():
                raise GenerationBlocked("live output must not exist")
            data = execute_approved(approval, root, expected_plan_sha256=args.expected_plan_sha256,
                client_factory=client_factory, key_provider=key_provider, now=now)
        elif args.mode == "plan":
            if not args.model or args.budget_usd is None or args.approval or args.expected_plan_sha256:
                raise GenerationBlocked("offline plan requires explicit model and budget")
            output = artifact_path(root, args.output or Path("output/live-approval-proposal.json"))
            if output.exists():
                raise GenerationBlocked("plan output must not exist")
            proposal = create_proposal(root, model=args.model, budget_usd=args.budget_usd,
                max_request_bytes=args.max_request_bytes, max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout_seconds, now=now)
            data = proposal.model_dump(mode="json")
        else:
            if any((args.model, args.budget_usd is not None, args.approval, args.expected_plan_sha256)):
                raise GenerationBlocked("mock cannot accept live approval settings")
            output = artifact_path(root, args.output or Path("output/mock-run.json"))
            source, hierarchy, layer, batch, _ = load_inputs(root)
            result = QuizWorkflow(ScriptedQuizAdapter([batch])).run(source, hierarchy, layer, "unit.part",
                QuizPolicy(max_attempts=1, max_questions=1))
            data = {"status": result.status, "provider_mode": "mock", "request_count": 0,
                "result": result.model_dump(mode="json")}
        output.parent.mkdir(parents=True, exist_ok=True)
        # Live and plan output cannot overwrite an existing artifact.
        with output.open("w" if args.mode == "mock" else "x") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        status = "approval_proposed" if args.mode == "plan" else data["status"]
        print(f"mode={args.mode} status={status} network={'explicitly_approved' if args.mode == 'live' else 'disabled'}")
        raise SystemExit(1 if status == "failed_human_review" else 0)
    except (GenerationBlocked, OSError, ValueError, TypeError) as exc:
        # Never echo input, keys, provider exceptions or source paths.
        print("status=blocked error="+type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
