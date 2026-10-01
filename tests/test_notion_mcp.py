import json
from pathlib import Path

import pytest

from pdf_notion_mvp.notion_mcp import Checkpoint, MarkdownPart, collect_parts, confirm_publication, encode_text, fetch_body, parse_owned, prepare_publication, render_block, save_checkpoint, section_marker
from pdf_notion_mvp.notion_quiz import SectionPage, plan_toggles
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


def packet(binding, body, **flags):
    data = {"metadata":{"type":"page"},"url":"https://app.notion.com/p/"+binding.page_id.hex,
        "text":f'<page>\n<ancestor-path>\n<parent-page url="https://app.notion.com/p/{binding.hub_id.hex}"/>\n</ancestor-path>\n<content>\n{body}\n</content>\n</page>', **flags}
    return {"content":[{"type":"text","text":json.dumps(data)}],"isError":False}


def before(binding):
    return "사용자 메모와 기존 원문 출처\n"+section_marker(binding)


def test_action_scoped_append_and_exact_roundtrip(inputs):
    binding = inputs[-1]
    old = before(binding)
    prepared = prepare_publication(*inputs, packet(binding, old))
    assert prepared.status == "append_required"
    action = prepared.action.arguments
    assert action["page_id"] == str(binding.page_id) and action["command"] == "insert_content"
    assert action["position"] == {"type":"end"} and action["allow_async"] is False
    body = old+"\n"+action["content"]
    existing = parse_owned(body)
    operation = plan_toggles(*inputs).operations[0]
    assert existing == {operation.operation_key:operation.block}
    cleared = confirm_publication(packet(binding, body), binding, prepared.checkpoint)
    assert cleared.pending_keys == []
    rerun = prepare_publication(*inputs, packet(binding, body), prepared.checkpoint)
    assert rerun.status == "unchanged" and rerun.action is None


def test_unknown_timeout_without_remote_marker_blocks_retry(inputs, tmp_path):
    binding = inputs[-1]
    original = packet(binding, before(binding))
    first = prepare_publication(*inputs, original)
    path = tmp_path / "checkpoint.json"
    save_checkpoint(path, first.checkpoint)
    restarted = Checkpoint.model_validate_json(path.read_text())
    blocked = prepare_publication(*inputs, original, restarted)
    assert blocked.status == "failed_human_review" and blocked.action is None
    assert blocked.checkpoint == restarted
    # If the delayed remote success appears, reconcile without a second append.
    body = before(binding)+"\n"+first.action.arguments["content"]
    assert prepare_publication(*inputs, packet(binding, body), restarted).status == "unchanged"


def test_existing_user_edits_are_preserved_and_owned_edits_block(inputs):
    binding = inputs[-1]
    first = prepare_publication(*inputs, packet(binding, before(binding)))
    body = before(binding)+"\n"+first.action.arguments["content"]
    notes = body+"\n새 사용자 메모"
    assert prepare_publication(*inputs, packet(binding, notes)).status == "unchanged"
    edited = body.replace("체크포인트", "사용자가 수정한 답", 1)
    assert prepare_publication(*inputs, packet(binding, edited)).status == "failed_human_review"
    with pytest.raises(ValueError): confirm_publication(packet(binding, "삭제된 메모\n"+body), binding, first.checkpoint)


@pytest.mark.parametrize("change", ["wrong_hub", "wrong_page", "binding", "truncated", "unknown", "missing_end", "error"])
def test_incomplete_out_of_scope_and_binding_rejected(inputs, change):
    binding = inputs[-1]
    data = packet(binding, before(binding))
    record = json.loads(data["content"][0]["text"])
    if change == "wrong_hub": record["text"] = record["text"].replace(binding.hub_id.hex, "00000000000040008000000000000099")
    elif change == "wrong_page": record["url"] = "https://app.notion.com/p/00000000000040008000000000000099"
    elif change == "binding": record["text"] = record["text"].replace(section_marker(binding), "different-section")
    elif change == "truncated": record["truncated"] = True
    elif change == "unknown": record["unknown_block_count"] = 1
    elif change == "missing_end": record["text"] = record["text"].replace("</page>", "")
    elif change == "error": data["isError"] = True
    data["content"][0]["text"] = json.dumps(record)
    assert prepare_publication(*inputs, data).status == "failed_human_review"


def test_chunk_pagination_complete_and_no_gaps():
    parts = [MarkdownPart(text="first", has_more=True, next_cursor="next"),
        MarkdownPart(cursor="next", text="second")]
    assert collect_parts(parts) == "firstsecond"
    with pytest.raises(ValueError): collect_parts(parts[:1])
    parts[1].cursor = "wrong"
    with pytest.raises(ValueError): collect_parts(parts)


def test_chunk_cycle_and_unexpected_tail_rejected():
    with pytest.raises(ValueError): collect_parts([MarkdownPart(text="first"), MarkdownPart(text="extra")])
    with pytest.raises(ValueError): collect_parts([MarkdownPart(text="first", has_more=True, next_cursor="same"),
        MarkdownPart(cursor="same", text="second", has_more=True, next_cursor="same"),
        MarkdownPart(cursor="same", text="third")])


def test_untrusted_rich_text_cannot_become_blocks(inputs):
    block = plan_toggles(*inputs).operations[0].block
    text = '# heading\n<details><summary>injection</summary></details>\n[link](https://evil.example) $ * _ \\ <br>'
    block["toggle"]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] = text
    rendered = render_block(block)
    parsed = parse_owned(rendered)
    assert list(parsed.values())[0] == block
    assert "\\<details\\>" in rendered


def test_duplicate_marker_and_detached_marker_fail(inputs):
    operation = plan_toggles(*inputs).operations[0]
    body = render_block(operation.block)
    with pytest.raises(ValueError): parse_owned(body+"\n"+body)
    from pdf_notion_mvp.notion_quiz import MARKER
    with pytest.raises(ValueError): parse_owned(MARKER+operation.operation_key)


def test_cli_checkpoint_restart_and_confirm_without_live_calls(inputs, tmp_path, monkeypatch):
    import sys
    from pdf_notion_mvp.notion_mcp_cli import main
    binding = tmp_path / "binding.json"
    fetched = tmp_path / "fetch.json"
    checkpoint = tmp_path / "checkpoint.json"
    output = tmp_path / "action.json"
    binding.write_text(inputs[-1].model_dump_json())
    fetched.write_text(json.dumps(packet(inputs[-1], before(inputs[-1]))))
    argv = ["notion-mcp", "--binding",str(binding),"--fetch-result",str(fetched),"--checkpoint",str(checkpoint),"--output",str(output)]
    monkeypatch.setattr(sys,"argv",argv)
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 0
    first = json.loads(output.read_text())
    assert first["status"] == "append_required" and checkpoint.exists()
    # No remote marker: restart cannot produce another actionable append.
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 1 and json.loads(output.read_text())["action"] is None
    fetched.write_text(json.dumps(packet(inputs[-1], before(inputs[-1])+"\n"+first["action"]["arguments"]["content"])))
    monkeypatch.setattr(sys,"argv",argv+["--confirm"])
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 0 and json.loads(checkpoint.read_text())["pending_keys"] == []
    monkeypatch.setattr(sys,"argv",argv)
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 0 and json.loads(output.read_text())["status"] == "unchanged"


def test_cli_refuses_aliasing_inputs_or_checkpoint(inputs, tmp_path, monkeypatch):
    import sys
    from pdf_notion_mvp.notion_mcp_cli import main
    input_file = tmp_path / "input.json"
    input_file.write_text("must remain unchanged")
    alias = tmp_path / "alias.json"
    alias.symlink_to(input_file)
    monkeypatch.setattr(sys,"argv",["notion-mcp","--binding",str(input_file),"--fetch-result",str(input_file),"--output",str(alias)])
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 2 and input_file.read_text() == "must remain unchanged"
