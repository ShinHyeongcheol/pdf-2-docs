from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from pdf_notion_mvp.notion_quiz import MemoryPageGateway, PageSnapshot, SectionPage, owned_key, paragraph, plan_toggles, publish_mock
from pdf_notion_mvp.openai_adapter import ScriptedQuizAdapter
from pdf_notion_mvp.quiz import QuizBatch, QuizWorkflow
from pdf_notion_mvp.review import HierarchicalOutline, ReviewLayer


@pytest.fixture
def inputs(source):
    root = Path(__file__).parents[1] / "fixtures"
    hierarchy = HierarchicalOutline.model_validate_json((root / "synthetic-outline.json").read_text())
    layer = ReviewLayer.model_validate_json((root / "synthetic-review.json").read_text())
    batch = QuizBatch.model_validate_json((root / "synthetic-quiz.json").read_text())
    result = QuizWorkflow(ScriptedQuizAdapter([batch])).run(source, hierarchy, layer, "unit.part")
    binding = SectionPage(hub_id="00000000-0000-4000-8000-000000000001", page_id="00000000-0000-4000-8000-000000000002",
        document_id=source.document.document_id, version=source.document.version, section_id="unit.part")
    return source, hierarchy, layer, result, binding


def publish(inputs, gateway):
    return publish_mock(*inputs, gateway)


def texts(block):
    kind = block["type"]
    parts = [r["text"]["content"] for r in block[kind]["rich_text"]]
    for child in block[kind].get("children", []): parts.extend(texts(child))
    return parts


def test_native_payload_provenance_and_immutable_inputs(inputs):
    before = [i.model_dump_json() for i in inputs]
    plan = plan_toggles(*inputs)
    block = plan.operations[0].block
    children = block["toggle"]["children"]
    assert block["type"] == "toggle"
    assert children[1]["toggle"]["rich_text"][0]["text"]["content"] == "정답"
    assert children[2]["toggle"]["rich_text"][0]["text"]["content"] == "해설과 원문 출처"
    content = "\n".join(texts(block))
    assert "[모의 문제 · 검토 필요]" in content and "실제 모델 생성" in content
    assert '"block_id": "b7"' in content and '"bbox"' in content and '"page": 2' in content
    assert inputs[3].batch.questions[0].explanation in content
    assert owned_key(block) == plan.operations[0].operation_key
    assert before == [i.model_dump_json() for i in inputs]


def test_sequential_rerun_and_user_notes_preserved(inputs):
    notes = [paragraph("내 메모: 그대로 보존"), paragraph("기존 원문 출처")]
    gateway = MemoryPageGateway(inputs[-1], notes)
    first = publish(inputs, gateway)
    gateway.children.append(paragraph("문제 풀이 후 사용자 메모"))
    saved = deepcopy(gateway.children)
    second = publish(inputs, gateway)
    assert first.status == "mock_written" and len(first.appended_keys) == 1
    assert second.status == "unchanged" and second.existing_keys == first.appended_keys
    assert gateway.append_calls == 1 and gateway.children == saved
    assert gateway.children[:2] == notes


def test_ambiguous_remote_success_then_restart_reconciles(inputs):
    class TimeoutAfterAppend(MemoryPageGateway):
        def append(self, binding, block):
            super().append(binding, block)
            raise TimeoutError("secret should not be logged")
    gateway = TimeoutAfterAppend(inputs[-1])
    first = publish(inputs, gateway)
    second = publish(inputs, gateway)
    assert first.status == "failed_human_review" and first.errors == ["publish_error:TimeoutError"]
    assert second.status == "unchanged" and gateway.append_calls == 1


@pytest.mark.parametrize("change", ["status", "reference", "claim", "source_digest", "evidence_digest", "version", "provider"])
def test_saved_ready_result_is_reverified_before_write(inputs, change):
    source, hierarchy, layer, result, binding = inputs
    result = result.model_copy(deep=True)
    if change == "status": result.status = "failed_human_review"
    elif change == "reference": result.batch.questions[0].source_block_ids = ["missing"]
    elif change == "claim": result.batch.questions[0].explanation = "근거 없는 주장"
    elif change == "provider": result.provider_mode = "openai"
    else: setattr(result, change, "wrong")
    gateway = MemoryPageGateway(binding)
    with pytest.raises(ValueError): publish_mock(source, hierarchy, layer, result, binding, gateway)
    assert gateway.children == [] and gateway.append_calls == 0


@pytest.mark.parametrize("change", ["hub", "page", "section", "incomplete"])
def test_binding_and_complete_scan_required(inputs, change):
    class WrongSnapshot(MemoryPageGateway):
        def snapshot(self, binding):
            snapshot = super().snapshot(binding)
            if change == "incomplete": snapshot.complete = False
            elif change == "section": snapshot.binding.section_id = "different"
            else: setattr(snapshot.binding, change+"_id", UUID("00000000-0000-4000-8000-000000000099"))
            return snapshot
    gateway = WrongSnapshot(inputs[-1])
    assert publish(inputs, gateway).status == "failed_human_review"
    assert gateway.append_calls == 0


def test_remote_user_edit_conflict_is_not_overwritten(inputs):
    gateway = MemoryPageGateway(inputs[-1])
    publish(inputs, gateway)
    gateway.children[0]["toggle"]["children"].append(paragraph("내 문제 풀이 메모"))
    saved = deepcopy(gateway.children)
    assert publish(inputs, gateway).status == "failed_human_review"
    assert gateway.children == saved and gateway.append_calls == 1


def test_duplicate_remote_marker_stops_without_append(inputs):
    block = plan_toggles(*inputs).operations[0].block
    gateway = MemoryPageGateway(inputs[-1], [block, block])
    assert publish(inputs, gateway).status == "failed_human_review"
    assert gateway.append_calls == 0


def test_provider_question_id_changes_do_not_duplicate(inputs):
    gateway = MemoryPageGateway(inputs[-1])
    publish(inputs, gateway)
    inputs[3].batch.questions[0].question_id = "new-provider-assigned-id"
    assert publish(inputs, gateway).status == "unchanged"
    assert gateway.append_calls == 1


def test_no_live_gateway_calls(inputs):
    class Live:
        mode = "live"
        def snapshot(self, binding): raise AssertionError("must not call")
        def append(self, binding, block): raise AssertionError("must not call")
    assert publish(inputs, Live()).status == "failed_human_review"


def test_size_limit_before_write(inputs):
    # Inflate a non-cited part of the same evidence; independently valid quiz still passes.
    from pdf_notion_mvp.review import document_digest
    from pdf_notion_mvp.quiz import prepare_context
    import hashlib
    inputs[0].document.blocks[-1].text += "길" * 21_000
    inputs[2].source_digest = document_digest(inputs[0].document)
    context = prepare_context(*inputs[:3], "unit.part")
    inputs[3].source_digest = context.source_digest
    inputs[3].evidence_digest = hashlib.sha256(context.model_dump_json().encode()).hexdigest()
    gateway = MemoryPageGateway(inputs[-1])
    with pytest.raises(ValueError, match="limit"): publish(inputs, gateway)
    assert gateway.append_calls == 0
