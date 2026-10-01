from pdf_notion_mvp.workflow import PIPELINE_VERSION
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.runnables import RunnableSequence

from pdf_notion_mvp.adapters import DryRunNotionPlanner, FixtureExtractor, LocalKnowledgeAdapter
from pdf_notion_mvp.api import default_workflow
from pdf_notion_mvp.contracts import Status
from pdf_notion_mvp.store import ConflictError, JobStore
from pdf_notion_mvp.workflow import Workflow, transition


def test_end_to_end_preserves_all_types_and_provenance(tmp_path, source):
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "request-1", PIPELINE_VERSION)
    workflow = default_workflow()
    assert isinstance(workflow.knowledge.planning_chain, RunnableSequence)
    assert "verify" in workflow.graph.get_graph().nodes
    for status in (Status.EXTRACTED, Status.PLANNED, Status.RESTORED, Status.VALIDATED, Status.READY):
        job = store.advance(job.job_id, workflow, job.revision)
        assert job.status == status
    assert job.validation.passed
    assert len(job.outline.sections) == 2
    assert [o.block.model_dump() for o in job.publish_plan.operations] == [b.model_dump() for b in source.document.blocks]
    assert len({o.operation_key for o in job.publish_plan.operations}) == 7
    assert job.publish_plan.mode == "dry_run" and job.publish_plan.target is None
    assert store.run(job.job_id, workflow) == job  # no duplicate operations/history


def test_reopen_resumes_without_repeating_extraction(tmp_path, source):
    path = tmp_path / "jobs.sqlite"
    store = JobStore(path)
    job = store.create(source, "a", PIPELINE_VERSION)
    job = store.advance(job.job_id, default_workflow())

    class NeverExtract:
        def extract(self, source):
            raise AssertionError("checkpoint must skip extraction")

    workflow = Workflow(NeverExtract(), LocalKnowledgeAdapter(), DryRunNotionPlanner())
    result = JobStore(path).run(job.job_id, workflow)
    assert result.status == Status.READY
    assert result.revision == 5


def test_failure_resume_and_error_redaction(tmp_path, source):
    class FlakyKnowledge(LocalKnowledgeAdapter):
        fail = True

        def plan(self, document):
            if self.fail:
                raise RuntimeError("sensitive-provider-payload")
            return super().plan(document)

    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    knowledge = FlakyKnowledge()
    workflow = Workflow(FixtureExtractor(), knowledge, DryRunNotionPlanner())
    failed = store.run(job.job_id, workflow)
    assert failed.status == Status.FAILED and failed.last_success == Status.EXTRACTED
    assert failed.ir == source.document and failed.outline is None
    assert failed.error == "plan: RuntimeError"
    assert "sensitive-provider-payload" not in failed.model_dump_json()
    assert store.advance(job.job_id, workflow) == failed
    resumed = JobStore(store.path).resume(job.job_id, failed.revision)
    assert resumed.status == Status.EXTRACTED and resumed.error is None
    knowledge.fail = False
    result = store.run(job.job_id, workflow)
    assert result.status == Status.READY
    assert sum(e.to_status == Status.EXTRACTED and e.from_status == Status.QUEUED for e in result.history) == 1


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "order", "content", "source", "section"])
def test_independent_verifier_blocks_corrupt_restore(tmp_path, source, corruption):
    class BadKnowledge(LocalKnowledgeAdapter):
        def restore(self, document, outline):
            result = super().restore(document, outline)
            blocks = result.sections[0].blocks
            if corruption == "missing": blocks.pop()
            elif corruption == "duplicate": blocks.append(blocks[-1].model_copy(deep=True))
            elif corruption == "order": blocks.reverse()
            elif corruption == "content": blocks[1].text = "changed"
            elif corruption == "source": blocks[1].source.version = "wrong"
            elif corruption == "section": result.sections[0].title = "changed"
            return result

    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    result = store.run(job.job_id, Workflow(FixtureExtractor(), BadKnowledge(), DryRunNotionPlanner()))
    assert result.status == Status.REJECTED
    assert result.validation.errors and not result.validation.passed
    assert result.publish_plan is None
    assert store.advance(job.job_id, default_workflow()) == result
    with pytest.raises(ConflictError): store.resume(job.job_id)


def test_idempotency_and_version_isolation(tmp_path, source):
    store = JobStore(tmp_path / "jobs.sqlite")
    first = store.create(source, "a", PIPELINE_VERSION)
    assert store.create(source, "a", PIPELINE_VERSION).job_id == first.job_id
    assert store.create(source, "b", PIPELINE_VERSION).job_id == first.job_id
    assert store.create(source, "c", "v2").job_id != first.job_id
    changed = source.model_copy(deep=True)
    changed.document.blocks[1].text += " edited"
    assert store.create(changed, "d", PIPELINE_VERSION).job_id != first.job_id
    with pytest.raises(ConflictError): store.create(changed, "a", PIPELINE_VERSION)


def test_concurrent_creation_deduplicates(tmp_path, source):
    path = tmp_path / "jobs.sqlite"
    store = JobStore(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda n: store.create(source, str(n), PIPELINE_VERSION).job_id, range(8)))
    assert len(set(ids)) == 1


def test_concurrent_advance_checks_revision(tmp_path, source):
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    def advance(_):
        try: return store.advance(job.job_id, default_workflow(), 0).status
        except ConflictError: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(advance, range(2)))
    assert results.count(Status.EXTRACTED) == 1 and results.count("conflict") == 1
    assert store.get(job.job_id).revision == 1


def test_interruption_rolls_back_stage(tmp_path, source):
    class Interrupt:
        pipeline_version = PIPELINE_VERSION
        def advance(self, job):
            job.status = Status.READY
            raise KeyboardInterrupt()
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "a", PIPELINE_VERSION)
    with pytest.raises(KeyboardInterrupt): store.advance(job.job_id, Interrupt())
    assert JobStore(store.path).get(job.job_id) == job
    assert store.run(job.job_id, default_workflow()).status == Status.READY


def test_invalid_transitions_are_rejected(tmp_path, source):
    job = JobStore(tmp_path / "jobs.sqlite").create(source, "a", PIPELINE_VERSION)
    with pytest.raises(ValueError): transition(job, Status.READY)
    with pytest.raises(ValueError): transition(job, Status.EXTRACTED, resume=True)
