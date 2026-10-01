import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdf_notion_mvp.graph_cli import load_graph_fixture, main
from pdf_notion_mvp.graph_review import (
    GRAPH_MARKER, GraphDraft, GraphObservation, ScriptedGraphAdapter, generate_graph,
    graph_owned_key, plan_graph_toggle, prepare_graph_context, publish_graph_mock,
)
from pdf_notion_mvp.notion_quiz import MemoryPageGateway, PageSnapshot, paragraph, toggle

ROOT = Path(__file__).parents[1]


@pytest.fixture
def graph():
    return load_graph_fixture(ROOT)


def inputs(graph):
    f, image = graph
    return f.source, f.hierarchy, f.review, f.evidence, image


def generate(graph, response=None):
    return generate_graph(*inputs(graph), ScriptedGraphAdapter(graph[0].mock_draft if response is None else response))


def test_graph_asset_bytes_are_in_mock_context_and_source_is_unchanged(graph):
    import base64
    f, image = graph
    before = f.model_dump_json()
    class Capturing(ScriptedGraphAdapter):
        def respond(self, context):
            assert base64.b64decode(context.image_base64) == image
            assert context.image.sha256 == f.evidence.image_sha256
            return super().respond(context)
    adapter = Capturing(f.mock_draft)
    result = generate_graph(*inputs(graph), adapter)
    assert result.status == 'ready_for_review' and adapter.calls == 1
    assert result.events == ['mock_multimodal_generate', 'independent_validate']
    assert f.model_dump_json() == before
    assert result.human_review_required and not result.actual_vision_verified
    plan = plan_graph_toggle(*inputs(graph), result, f.binding)
    text = json.dumps(plan.model_dump(mode='json'), ensure_ascii=False)
    for expected in ['추출 후보 · 미확정','확인된 관찰 · 합성 근거','모의 모델 추론 후보 · 미확정',
                     '검토 필요 사항','판독 불가','A=10, B=20, C=15','graph-image','authored-v1','bbox','page']:
        assert expected in text
    key = graph_owned_key(plan.operations[0].block)
    assert key == plan.operations[0].operation_key
    assert not plan.actual_vision_verified and not plan.semantic_correctness_verified


@pytest.mark.parametrize('change', ['source','version','section','parent','block','asset','duplicate','outside','mode','empty','media','malformed_svg','oversized_asset','entity'])
def test_context_rejects_stale_or_unowned_evidence_before_generation(graph, change):
    f, image = graph
    if change == 'source': f.evidence.source_digest = '0'*64
    if change == 'version': f.evidence.version = 'other'
    if change == 'section': f.evidence.section_id = 'missing'
    if change == 'parent': f.evidence.section_id = 'graphs'
    if change == 'block': f.evidence.image_block_id = 'graph-heading'
    if change == 'asset': image += b' '
    if change == 'duplicate': f.evidence.observations.append(f.evidence.observations[0].model_copy(deep=True))
    if change == 'outside': f.evidence.observations[0].bbox.x1 = 599
    if change == 'mode': f.source.kind = 'ocr_ir'
    if change == 'empty': image = b''
    if change == 'media': image = b'<html>not an authored SVG</html>'
    if change == 'malformed_svg': image = b'<svg'
    if change == 'oversized_asset': image = b'x'*100_001
    if change == 'entity': image = b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'
    adapter = ScriptedGraphAdapter(f.mock_draft)
    with pytest.raises(ValueError): generate_graph(*inputs((f,image)), adapter)
    assert adapter.calls == 0


@pytest.mark.parametrize('change', ['new_value','new_trend','promotion','unreadable','omit','wrong_image','omit_review',
                                    'candidate_basis','unreadable_basis','label_basis','unknown_basis','duplicate_basis','duplicate_inference'])
def test_independent_verifier_blocks_unsupported_model_claims(graph, change):
    draft = graph[0].mock_draft.model_dump(mode='json')
    if change == 'new_value': draft['new_numeric_fact'] = 'A=999'
    if change == 'new_trend': draft['trend'] = 'all values increase'
    if change == 'promotion': draft['confirmed_observation_ids'].append('unit-candidate')
    if change == 'unreadable': draft['confirmed_observation_ids'].append('unknown-tick')
    if change == 'omit': draft['confirmed_observation_ids'] = []
    if change == 'wrong_image': draft['image_block_id'] = 'other'
    if change == 'omit_review': draft['review_reasons'] = []
    basis = {'candidate_basis':'unit-candidate','unreadable_basis':'unknown-tick','label_basis':'labels','unknown_basis':'new'}
    if change in basis: draft['inference_candidates'][0]['basis_observation_ids'] = [basis[change]]
    if change == 'duplicate_basis': draft['inference_candidates'][0]['basis_observation_ids'] = ['values','values']
    if change == 'duplicate_inference': draft['inference_candidates'] *= 2
    result = generate(graph, draft)
    assert result.status == 'failed_human_review' and result.draft is None
    with pytest.raises(ValueError): plan_graph_toggle(*inputs(graph), result, graph[0].binding)


@pytest.mark.parametrize('change', ['candidate_confirmed','unreadable_value','unreadable_trend'])
def test_observation_contract_never_promotes_mock_extraction(graph, change):
    obs = graph[0].evidence.observations[-1 if change.startswith('unreadable') else -2].model_dump()
    if change == 'candidate_confirmed': obs['status'] = 'confirmed'
    else:
        obs['text'] = 'invented 999' if change == 'unreadable_value' else 'invented upward trend'
        obs['kind'] = 'value' if change == 'unreadable_value' else 'trend'
    with pytest.raises(ValidationError): GraphObservation.model_validate(obs)


def test_no_confirmed_evidence_produces_review_only_without_fabricated_values(graph):
    f, image = graph
    f.evidence.observations = [o for o in f.evidence.observations if o.status != 'confirmed']
    draft = GraphDraft(image_block_id=f.evidence.image_block_id, confirmed_observation_ids=[],
        extracted_observation_ids=['unit-candidate'], inference_candidates=[],
        review_reasons=['semantic_unverified','no_confirmed_observations','unreadable_values','extracted_candidates'])
    result = generate(graph, draft)
    assert result.status == 'ready_for_review'
    payload = plan_graph_toggle(*inputs(graph), result, f.binding).operations[0].block
    confirmed = payload['toggle']['children'][3]['toggle']['children']
    assert confirmed == []
    assert '확인된 관찰이 없어' in json.dumps(payload,ensure_ascii=False)
    unsupported = draft.model_dump()
    unsupported['inference_candidates'] = [{'kind':'measurement_context_needed','basis_observation_ids':['unknown-tick']}]
    assert generate(graph, unsupported).status == 'failed_human_review'


def test_generator_mutation_cannot_change_independent_authority(graph):
    f, _ = graph
    class Mutating:
        mode = 'mock_multimodal'
        def generate(self, context):
            context.evidence.observations[0].text = 'A=999'
            context.evidence.observations[-2].status = 'confirmed'
            data = f.mock_draft.model_dump()
            data['confirmed_observation_ids'].append('unit-candidate')
            return data
    before = f.model_dump_json()
    assert generate_graph(*inputs(graph), Mutating()).status == 'failed_human_review'
    assert f.model_dump_json() == before


def test_live_adapter_blocked_without_generation_or_key_access(graph):
    class Live:
        mode = 'gemini'
        def generate(self, context): raise AssertionError('must never be called')
    result = generate_graph(*inputs(graph), Live())
    assert result.status == 'failed_human_review' and result.events == []


def test_inherited_langsmith_tracing_is_disabled(graph, monkeypatch):
    from langsmith import tracing_context
    from langsmith.run_helpers import get_tracing_context
    monkeypatch.setenv('LANGSMITH_TRACING','true')
    monkeypatch.setenv('LANGSMITH_API_KEY','fabricated-tracing-key')
    class Checked(ScriptedGraphAdapter):
        def respond(self, context):
            assert get_tracing_context()['enabled'] is False
            return super().respond(context)
    with tracing_context(enabled=True):
        result = generate_graph(*inputs(graph), Checked(graph[0].mock_draft))
    assert result.status == 'ready_for_review'


@pytest.mark.parametrize('change', ['source','evidence','image','events','errors','section','binding','confirmed','flag','provider'])
def test_saved_results_revalidated_before_any_publish(graph, change):
    f, _ = graph
    result = generate(graph)
    if change == 'source': result.source_digest = '0'*64
    if change == 'evidence': f.evidence.observations[0].text = 'A=999'
    if change == 'image': result.image_sha256 = '0'*64
    if change == 'events': result.events = ['mock_multimodal_generate']
    if change == 'errors': result.errors = ['bad']
    if change == 'section': result.section_id = 'missing'
    if change == 'binding': f.binding.version = 'other'
    if change == 'confirmed': result.draft.confirmed_observation_ids.append('unit-candidate')
    if change == 'flag': result.actual_vision_verified = True
    if change == 'provider': result.provider_mode = 'actual'
    gateway = MemoryPageGateway(f.binding)
    with pytest.raises(ValueError): publish_graph_mock(*inputs(graph), result, f.binding, gateway)
    assert gateway.append_calls == 0


def test_mock_publish_restart_preserves_notes_and_other_app_markers(graph):
    f, _ = graph
    result = generate(graph)
    note = paragraph('사용자가 작성한 메모')
    quiz = toggle('기존 합성 문제',[toggle('출처',[paragraph('pdf-notion-quiz:v1:'+'a'*64)])])
    gateway = MemoryPageGateway(f.binding,[note,quiz])
    first = publish_graph_mock(*inputs(graph), result, f.binding, gateway)
    restarted = MemoryPageGateway(f.binding,gateway.snapshot(f.binding).children)
    second = publish_graph_mock(*inputs(graph), result, f.binding, restarted)
    assert (first.status,second.status) == ('mock_written','unchanged')
    assert gateway.children[:2] == [note,quiz] and restarted.append_calls == 0
    # Editing a returned plan cannot replace authoritative inputs used by publisher.
    plan = plan_graph_toggle(*inputs(graph), result, f.binding)
    plan.operations[0].block = paragraph('tampered plan')
    assert publish_graph_mock(*inputs(graph), result, f.binding, restarted).status == 'unchanged'


@pytest.mark.parametrize('change',['edited','duplicate','malformed','incomplete','binding','live'])
def test_page_conflicts_and_live_gateways_fail_before_writes(graph,change):
    f,_ = graph
    result = generate(graph)
    block = plan_graph_toggle(*inputs(graph),result,f.binding).operations[0].block
    if change == 'edited': block['toggle']['rich_text'][0]['text']['content'] = '사용자 편집'
    if change == 'malformed': block['toggle']['children'][-1]['toggle']['children'][0] = paragraph(GRAPH_MARKER+'broken')
    children = [block,deepcopy(block)] if change == 'duplicate' else [block]
    class Gateway(MemoryPageGateway):
        def snapshot(self,binding):
            if change == 'live': raise AssertionError('live gateway must never be read')
            snapshot = super().snapshot(binding)
            if change == 'incomplete': snapshot.complete = False
            if change == 'binding': snapshot.binding.section_id = 'other'
            return snapshot
    gateway = Gateway(f.binding,children)
    if change == 'live': gateway.mode = 'live'
    original = deepcopy(gateway.children)
    receipt = publish_graph_mock(*inputs(graph),result,f.binding,gateway)
    assert receipt.status == 'failed_human_review'
    assert gateway.append_calls == 0 and gateway.children == original


def test_ambiguous_mock_append_reconciles_from_marker_on_next_run(graph):
    f,_ = graph
    class Timeout(MemoryPageGateway):
        def append(self,binding,block):
            super().append(binding,block)
            raise TimeoutError('fabricated-secret-not-for-logs')
    gateway = Timeout(f.binding,[paragraph('기존 메모')])
    result = generate(graph)
    first = publish_graph_mock(*inputs(graph),result,f.binding,gateway)
    second = publish_graph_mock(*inputs(graph),result,f.binding,gateway)
    assert (first.status,second.status) == ('failed_human_review','unchanged')
    assert gateway.append_calls == 1 and len(gateway.children) == 2
    assert first.errors == ['graph_publish_error:TimeoutError']


@pytest.fixture
def cli_root(tmp_path):
    (tmp_path/'fixtures').mkdir()
    for name in ['synthetic-graph.json','synthetic-graph.svg']:
        (tmp_path/'fixtures'/name).write_bytes((ROOT/'fixtures'/name).read_bytes())
    return tmp_path


def run_cli(root,args):
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    return exc.value.code


def test_cli_plan_only_then_persistent_restart_without_credentials(cli_root,monkeypatch):
    original_read = Path.read_text
    def reject_dotenv(path,*args,**kwargs):
        assert not path.name.startswith('.env')
        return original_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',reject_dotenv)
    monkeypatch.setattr('pdf_notion_mvp.providers.build_provider',lambda *a,**k: pytest.fail('provider must never be built'))
    assert run_cli(cli_root,[]) == 0
    page = cli_root/'output/mock-graph-page.json'
    assert not page.exists()
    assert run_cli(cli_root,['--mock-publish']) == 0
    snapshot = PageSnapshot.model_validate_json(page.read_text())
    note = paragraph('추가 사용자 메모')
    snapshot.children.insert(0,note)
    page.write_text(snapshot.model_dump_json())
    assert run_cli(cli_root,['--mock-publish']) == 0
    saved = page.read_bytes()
    data = json.loads((cli_root/'output/mock-graph-plan.json').read_text())
    assert data['status'] == 'unchanged' and data['mock_adapter_calls'] == 1
    assert data['actual_model_requests'] == data['actual_key_reads'] == data['notion_requests'] == 0
    assert PageSnapshot.model_validate_json(saved).children[0] == note
    assert run_cli(cli_root,['--mock-publish']) == 0 and page.read_bytes() == saved


@pytest.mark.parametrize('args', [
    ['--output','fixtures/synthetic-graph.json'], ['--output','output/live-runs/a.json'],
    ['--output','output/plan.txt'], ['--output','../escape.json'],
    ['--output','output/same.json','--mock-page','output/same.json'],
])
def test_cli_rejects_unsafe_artifacts(cli_root,args):
    before=(cli_root/'fixtures/synthetic-graph.json').read_bytes()
    assert run_cli(cli_root,args) == 1
    assert before == (cli_root/'fixtures/synthetic-graph.json').read_bytes()


@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_cli_rejects_linked_output(cli_root,kind):
    import os
    (cli_root/'output').mkdir()
    source = cli_root/'fixtures/synthetic-graph.json'
    output = cli_root/'output/linked.json'
    before = source.read_bytes()
    if kind == 'symlink': output.symlink_to(source)
    else: os.link(source,output)
    assert run_cli(cli_root,['--output','output/linked.json']) == 1
    assert source.read_bytes() == before


@pytest.mark.parametrize('failure',['partial_write','replace','output_after_page'])
def test_atomic_page_save_failures_preserve_notes_and_reconcile_on_restart(cli_root,monkeypatch,capsys,failure):
    from pdf_notion_mvp import graph_cli
    assert run_cli(cli_root,['--mock-publish']) == 0
    page = cli_root/'output/mock-graph-page.json'
    snapshot = PageSnapshot.model_validate_json(page.read_text())
    snapshot.children = [paragraph('유실되면 안 되는 사용자 메모')]
    page.write_text(snapshot.model_dump_json())
    original = page.read_bytes()
    old_dump = json.dump
    old_replace = Path.replace
    def partial(value,stream,*args,**kwargs):
        if Path(stream.name).name.startswith('.mock-graph-page.json-'):
            stream.write('{"incomplete":')
            stream.flush()
            raise OSError('fabricated-secret-not-for-logs')
        return old_dump(value,stream,*args,**kwargs)
    def fail_replace(path,target):
        if Path(target).name == ('mock-graph-plan.json' if failure == 'output_after_page' else page.name):
            raise OSError('fabricated-secret-not-for-logs')
        return old_replace(path,target)
    with monkeypatch.context() as scoped:
        if failure == 'partial_write': scoped.setattr(graph_cli.json,'dump',partial)
        else: scoped.setattr(Path,'replace',fail_replace)
        assert run_cli(cli_root,['--mock-publish']) == 1
    if failure != 'output_after_page': assert page.read_bytes() == original
    assert PageSnapshot.model_validate_json(page.read_text()).children[0] == snapshot.children[0]
    assert not list(page.parent.glob('.*.json-*'))
    assert run_cli(cli_root,['--mock-publish']) == 0
    saved = page.read_bytes()
    assert run_cli(cli_root,['--mock-publish']) == 0 and page.read_bytes() == saved
    assert len(PageSnapshot.model_validate_json(saved).children) == 2
    assert 'fabricated-secret-not-for-logs' not in capsys.readouterr().out


def test_mock_adapter_failure_requires_review_and_does_not_expose_error_text(graph):
    result = generate(graph, TimeoutError("fabricated-secret-not-for-logs"))
    assert result.status == "failed_human_review" and result.draft is None
    assert result.errors == ["graph_error:TimeoutError"]
    assert "fabricated-secret" not in result.model_dump_json()


def test_cli_conflict_keeps_user_edited_graph_file(cli_root):
    assert run_cli(cli_root,["--mock-publish"]) == 0
    page = cli_root/"output/mock-graph-page.json"
    snapshot = PageSnapshot.model_validate_json(page.read_text())
    snapshot.children[1]["toggle"]["rich_text"][0]["text"]["content"] = "사용자 그래프 편집"
    page.write_text(snapshot.model_dump_json())
    original = page.read_bytes()
    assert run_cli(cli_root,["--mock-publish"]) == 1
    assert page.read_bytes() == original
    assert json.loads((cli_root/"output/mock-graph-plan.json").read_text())["status"] == "failed_human_review"


@pytest.mark.parametrize('movement',['top_level_paragraph','extra_nested_toggle'])
def test_moved_graph_marker_requires_review_before_duplicate_append(graph,movement):
    f,_=graph
    result=generate(graph)
    note=paragraph('이동된 표식 검토 중 보존할 사용자 메모')
    quiz=toggle('기존 문제',[toggle('출처',[paragraph('pdf-notion-quiz:v1:'+'a'*64)])])
    gateway=MemoryPageGateway(f.binding,[note,quiz])
    assert publish_graph_mock(*inputs(graph),result,f.binding,gateway).status=='mock_written'
    identifier=gateway.children[2]['toggle']['children'][-1]
    marker=identifier['toggle']['children'].pop()
    if movement=='top_level_paragraph': gateway.children.append(marker)
    else: identifier['toggle']['children'].append(toggle('사용자가 추가한 중첩',[marker]))
    before=deepcopy(gateway.children)
    receipt=publish_graph_mock(*inputs(graph),result,f.binding,gateway)
    assert receipt.status=='failed_human_review'
    assert not receipt.appended_keys and gateway.append_calls==1
    assert gateway.children==before and gateway.children[:2]==[note,quiz]
