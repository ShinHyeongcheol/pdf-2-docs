"""Revalidate completed provider runs into review plans; all execution is mock Notion."""
import hashlib
from pathlib import Path

from .live_run import CompletedRunReceipt, RunApproval, RunCompletion, cost_envelope, fingerprint, load_inputs
from .notion_quiz import SectionPage, _reconcile, _render_verified_plan
from .quiz import prepare_context, verify_quiz


def validated_completion(project_root: Path, approval: RunApproval, completion: RunCompletion):
    approval = RunApproval.model_validate(approval.model_dump())
    completion = RunCompletion.model_validate(completion.model_dump())
    spec, result = approval.spec, completion.result
    if (approval.plan_sha256 != fingerprint(spec.model_dump(mode="json")) or
        not all((approval.user_approved, approval.data_transfer_confirmed, approval.pricing_and_limits_confirmed,
                 approval.model_capabilities_confirmed, approval.conditional_cost_understood))):
        raise ValueError("missing or changed generation approval")
    # This is read-back of a past completed run, not authorization for a new call.
    # Expiry is enforced at generation; publishing a past result does not renew it.
    if (completion.run_id != spec.run_id or completion.plan_sha256 != approval.plan_sha256 or
        completion.provider_mode != spec.model_snapshot.provider or
        result.provider_mode != completion.provider_mode or completion.status != result.status or
        completion.conditional_cost_envelope_usd != cost_envelope(spec)):
        raise ValueError("run/provider/approval mismatch")
    if (completion.status != "ready_for_review" or result.batch is None or result.errors or
        completion.request_count != 1 or result.attempts != 1 or
        result.events != ["generate", "independent_validate"]):
        raise ValueError("unvalidated or incomplete provider result")
    ledger = project_root.resolve() / "output" / "live-runs"
    path = ledger / (str(completion.run_id)+".json")
    if (ledger.is_symlink() or ledger.parent.is_symlink() or path.is_symlink() or
        not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 10_000):
        raise ValueError("executor-owned completed receipt required")
    receipt = CompletedRunReceipt.model_validate_json(path.read_text())
    completion_digest = fingerprint(completion.model_dump(mode="json"))
    if (receipt.run_id, receipt.plan_sha256, receipt.completion_sha256, receipt.provider, receipt.model,
        receipt.execution_mode, receipt.source_digest, receipt.evidence_digest, receipt.request_count) != (
        completion.run_id, approval.plan_sha256, completion_digest, completion.provider_mode,
        spec.model_snapshot.model, completion.execution_mode, result.source_digest, result.evidence_digest, 1):
        raise ValueError("result differs from executor-owned receipt")
    source, hierarchy, layer, _, input_digests = load_inputs(project_root)
    context = prepare_context(source, hierarchy, layer, spec.section_id)
    evidence_digest = hashlib.sha256(context.model_dump_json().encode()).hexdigest()
    if (input_digests != spec.input_digests or fingerprint(context.model_dump(mode="json")) != spec.context_digest or
        (result.document_id, result.version, result.section_id, result.source_digest, result.evidence_digest) !=
        (context.document_id, context.version, context.section_id, context.source_digest, evidence_digest)):
        raise ValueError("stale or mismatched publication source")
    if verify_quiz(context, result.batch, spec.max_questions):
        raise ValueError("provider quiz failed independent revalidation")
    return source, context, completion, completion_digest


def plan_provider_toggles(project_root: Path, approval: RunApproval, completion: RunCompletion,
                         binding: SectionPage):
    approval = RunApproval.model_validate(approval.model_dump())
    binding = SectionPage.model_validate(binding.model_dump())
    source, context, completion, completion_digest = validated_completion(project_root, approval, completion)
    if (binding.document_id, binding.version, binding.section_id) != (
        context.document_id, context.version, context.section_id):
        raise ValueError("section page binding mismatch")
    return _render_verified_plan(source, completion.result, binding, context,
        provider=completion.provider_mode, model=approval.spec.model_snapshot.model,
        execution_mode=completion.execution_mode, completion_sha256=completion_digest, run_id=completion.run_id)


def publish_provider_mock(project_root, approval, completion, binding, gateway):
    # The renderer's plan and a saved ready flag never bypass this revalidation boundary.
    return _reconcile(plan_provider_toggles(project_root, approval, completion, binding), gateway)
