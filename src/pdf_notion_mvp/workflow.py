from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .adapters import Extractor, KnowledgeAdapter, PublishPlanner
from .contracts import DocumentIR, Event, Job, Outline, PublishPlan, RestoredDocument, Status
from .verification import verify
from .identity import operation_key

PIPELINE_VERSION = "ir-preservation-v3"


ALLOWED = {
    Status.QUEUED: {Status.EXTRACTED, Status.FAILED},
    Status.EXTRACTED: {Status.PLANNED, Status.FAILED},
    Status.PLANNED: {Status.RESTORED, Status.FAILED},
    Status.RESTORED: {Status.VALIDATED, Status.REJECTED, Status.FAILED},
    Status.VALIDATED: {Status.READY, Status.FAILED},
    Status.READY: set(), Status.REJECTED: set(), Status.FAILED: set(),
}


def transition(job: Job, target: Status, *, resume: bool = False) -> Job:
    allowed = (job.status == Status.FAILED and target == job.last_success) if resume else target in ALLOWED[job.status]
    if not allowed:
        raise ValueError(f"invalid transition: {job.status} -> {target}")
    result = job.model_copy(deep=True)
    result.history.append(Event(
        from_status=job.status, to_status=target,
        timestamp=datetime.now(timezone.utc).isoformat(),
    ))
    result.status = target
    result.revision += 1
    if target not in {Status.FAILED, Status.REJECTED}:
        result.last_success = target
        result.error = None
    return result


class GraphState(TypedDict):
    job: Job


class Workflow:
    """Each graph invocation executes precisely one persisted stage."""

    def __init__(self, extractor: Extractor, knowledge: KnowledgeAdapter, publisher: PublishPlanner, pipeline_version: str = PIPELINE_VERSION, asset_root: Path | None = None):
        self.pipeline_version = pipeline_version
        self.asset_root = asset_root
        self.extractor = extractor
        self.knowledge = knowledge
        self.publisher = publisher
        graph = StateGraph(GraphState)
        nodes = {
            "extract": self.extract, "plan": self.plan, "restore": self.restore,
            "verify": self.validate, "publish_plan": self.publish,
        }
        for name, action in nodes.items():
            graph.add_node(name, action)
            graph.add_edge(name, END)
        graph.add_conditional_edges(START, self.route, {name: name for name in nodes} | {END: END})
        self.graph = graph.compile()

    @staticmethod
    def route(state: GraphState) -> str:
        return {
            Status.QUEUED: "extract", Status.EXTRACTED: "plan",
            Status.PLANNED: "restore", Status.RESTORED: "verify",
            Status.VALIDATED: "publish_plan",
        }.get(state["job"].status, END)

    def advance(self, job: Job) -> Job:
        if job.status in {Status.READY, Status.REJECTED, Status.FAILED}:
            return job
        try:
            return self.graph.invoke({"job": job.model_copy(deep=True)})["job"]
        except Exception as exc:
            failed = transition(job, Status.FAILED)
            # Never persist raw provider errors, which may contain credentials/payloads.
            failed.error = f"{self.route({'job': job})}: {type(exc).__name__}"
            return failed

    def extract(self, state: GraphState) -> GraphState:
        job = state["job"]
        extracted = self.extractor.extract(job.input.model_copy(deep=True))
        job.ir = DocumentIR.model_validate(extracted.model_dump())
        return {"job": transition(job, Status.EXTRACTED)}

    def plan(self, state: GraphState) -> GraphState:
        job = state["job"]
        assert job.ir is not None
        outline = self.knowledge.plan(job.ir.model_copy(deep=True))
        job.outline = Outline.model_validate(outline.model_dump())
        return {"job": transition(job, Status.PLANNED)}

    def restore(self, state: GraphState) -> GraphState:
        job = state["job"]
        assert job.ir is not None and job.outline is not None
        restored = self.knowledge.restore(job.ir.model_copy(deep=True), job.outline.model_copy(deep=True))
        job.restored = RestoredDocument.model_validate(restored.model_dump())
        return {"job": transition(job, Status.RESTORED)}

    def validate(self, state: GraphState) -> GraphState:
        job = state["job"]
        assert job.ir is not None and job.outline is not None and job.restored is not None
        # The original pre-extracted input is the independent immutable reference.
        job.validation = verify(job.input.document, job.outline, job.restored, self.asset_root)
        if job.ir.model_dump() != job.input.document.model_dump():
            job.validation.passed = False
            job.validation.errors.append("extracted IR differs from authoritative input IR")
        return {"job": transition(job, Status.VALIDATED if job.validation.passed else Status.REJECTED)}

    def publish(self, state: GraphState) -> GraphState:
        job = state["job"]
        if job.validation is None or not job.validation.passed or job.restored is None:
            raise ValueError("publication requires independent verification")
        plan = self.publisher.plan(job.restored.model_copy(deep=True), job.fingerprint)
        job.publish_plan = PublishPlan.model_validate(plan.model_dump())
        expected = [(s.section_id, s.title, b.model_dump()) for s in job.restored.sections for b in s.blocks]
        actual = [(o.section_id, o.section_title, o.block.model_dump()) for o in job.publish_plan.operations]
        if actual != expected or len({o.operation_key for o in job.publish_plan.operations}) != len(expected):
            raise ValueError("publishing plan differs from verified reconstruction")
        for operation in job.publish_plan.operations:
            if operation.operation_key != operation_key(job.fingerprint, operation.section_id, operation.block.block_id):
                raise ValueError("publishing operation key is not deterministic")
        return {"job": transition(job, Status.READY)}
