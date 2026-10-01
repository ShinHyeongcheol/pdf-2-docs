from pdf_notion_mvp.workflow import PIPELINE_VERSION
import pytest

from pdf_notion_mvp.adapters import DryRunNotionPlanner, FixtureExtractor, LocalKnowledgeAdapter
from pdf_notion_mvp.contracts import Status
from pdf_notion_mvp.store import JobStore
from pdf_notion_mvp.workflow import Workflow


@pytest.mark.parametrize("mutation", ["delete", "change", "add"])
def test_planner_cannot_mutate_reference_ir(tmp_path, source, mutation):
    class MutatingKnowledge(LocalKnowledgeAdapter):
        def plan(self, document):
            if mutation == "delete": document.blocks.pop()
            elif mutation == "change": document.blocks[1].text = "adapter changed text"
            elif mutation == "add":
                extra = document.blocks[-1].model_copy(deep=True)
                extra.block_id = "added-by-adapter"
                document.blocks.append(extra)
            return super().plan(document)
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    result = store.run(job.job_id, Workflow(FixtureExtractor(), MutatingKnowledge(), DryRunNotionPlanner()))
    assert result.input.document == source.document
    assert result.ir == source.document
    if mutation == "change":
        assert result.status == Status.READY
        assert result.publish_plan.operations[1].block.text == source.document.blocks[1].text
    else:
        assert result.status in {Status.REJECTED, Status.FAILED}
        assert result.publish_plan is None


def test_extractor_cannot_mutate_input_reference(tmp_path, source):
    class BadExtractor:
        def extract(self, source):
            source.document.blocks.pop()
            return source.document
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    result = store.run(job.job_id, Workflow(BadExtractor(), LocalKnowledgeAdapter(), DryRunNotionPlanner()))
    assert result.status == Status.REJECTED and result.publish_plan is None
    assert result.input.document == source.document
    assert "extracted IR differs from authoritative input IR" in result.validation.errors


def test_restorer_cannot_mutate_ir_or_outline(tmp_path, source):
    class BadRestorer(LocalKnowledgeAdapter):
        def restore(self, document, outline):
            document.blocks.pop()
            outline.sections[-1].block_ids.pop()
            return super().restore(document, outline)
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    result = store.run(job.job_id, Workflow(FixtureExtractor(), BadRestorer(), DryRunNotionPlanner()))
    assert result.status == Status.REJECTED and result.publish_plan is None
    assert result.ir == source.document and result.outline.sections[-1].block_ids[-1] == "b7"


def test_publisher_cannot_mutate_verified_reconstruction(tmp_path, source):
    class BadPublisher(DryRunNotionPlanner):
        def plan(self, restored, fingerprint):
            restored.sections[0].blocks.pop()
            return super().plan(restored, fingerprint)
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    result = store.run(job.job_id, Workflow(FixtureExtractor(), LocalKnowledgeAdapter(), BadPublisher()))
    assert result.status == Status.FAILED and result.publish_plan is None
    assert result.restored.sections[0].blocks[-1].block_id == "b5"


def test_mutated_invalid_restore_does_not_poison_checkpoint(tmp_path, source):
    class InvalidRestorer(LocalKnowledgeAdapter):
        def restore(self, document, outline):
            restored = super().restore(document, outline)
            restored.sections[0].blocks.clear()
            return restored
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    failed = store.run(job.job_id, Workflow(FixtureExtractor(), InvalidRestorer(), DryRunNotionPlanner()))
    assert failed.status == Status.FAILED and failed.last_success == Status.PLANNED
    assert failed.restored is None
    assert JobStore(store.path).get(job.job_id) == failed
    assert store.resume(job.job_id).status == Status.PLANNED
    assert store.run(job.job_id, Workflow(FixtureExtractor(), LocalKnowledgeAdapter(), DryRunNotionPlanner())).status == Status.READY


@pytest.mark.parametrize("mutation", ["empty", "duplicate_key", "wrong_key", "order", "content"])
def test_invalid_publish_plan_is_never_ready(tmp_path, source, mutation):
    class BadPublisher(DryRunNotionPlanner):
        def plan(self, restored, fingerprint):
            plan = super().plan(restored, fingerprint)
            if mutation == "empty": plan.operations.clear()
            elif mutation == "duplicate_key": plan.operations[1].operation_key = plan.operations[0].operation_key
            elif mutation == "wrong_key": plan.operations[0].operation_key = "random"
            elif mutation == "order": plan.operations.reverse()
            elif mutation == "content": plan.operations[1].block.text = "changed"
            return plan
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    result = store.run(job.job_id, Workflow(FixtureExtractor(), LocalKnowledgeAdapter(), BadPublisher()))
    assert result.status == Status.FAILED and result.last_success == Status.VALIDATED
    assert result.publish_plan is None
    assert store.get(job.job_id).validation.passed


def test_operation_keys_have_unambiguous_boundaries():
    from pdf_notion_mvp.identity import operation_key
    assert operation_key("fingerprint", "s:a", "b") != operation_key("fingerprint", "s", "a:b")


def test_workflow_version_mismatch_cannot_advance(tmp_path, source):
    from pdf_notion_mvp.store import ConflictError
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", "obsolete-version")
    with pytest.raises(ConflictError):
        store.advance(job.job_id, Workflow(FixtureExtractor(), LocalKnowledgeAdapter(), DryRunNotionPlanner()))
    assert store.get(job.job_id) == job
