"""Teaching-contract, SDK request and publication regression tests; no live network."""
import copy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from pdf_notion_mvp.instructional_lesson import (InstructionalDraft, PEDAGOGY_CHECKS,
    review_ids, verify_instructional,render_teaching,render_instructional_html)
from pdf_notion_mvp.lesson_generation import (ContentReview, create_proposal,execute,
    expected_request,fingerprint,units_for,wire_schema,parse_draft,recover_positional_ids)
from pdf_notion_mvp.lesson_notion import prepare_lesson
from pdf_notion_mvp.study_notion import canonical,UploadedPageImage
from pdf_notion_mvp.rag_files import load_review_files
from pdf_notion_mvp.study_session import prepare as prepare_session
from test_lesson_generation import NOW,ROOT,inputs
from test_lesson_notion import reviewed,remote
from test_study_notion import PAGE


def teaching(context):
    citations=[{'unit_id':u['unit_id'],'quote':u['text']} for u in context['units']]
    def claim(key,text):return dict(claim_id=key,text=text,citations=citations)
    return InstructionalDraft.model_validate(dict(lesson_format='instructional_v1',
        title='중단된 작업을 안전하게 이어가기',introduction='성공한 단계와 실패한 단계를 구분해 다시 시작하는 흐름을 배웁니다.',
        objectives=['저장된 결과와 작업 상태를 구분해 설명할 수 있습니다.'],teaching_units=[dict(
            teaching_id='resume',title='상태를 기록하면 무엇을 다시 실행할까?',
            definition=claim('definition','체크포인트는 완료한 작업을 기록한 자료입니다.'),
            purpose=claim('purpose','이미 성공한 단계를 반복하지 않고, 남은 작업부터 이어가기 위해 사용합니다.'),
            mechanism=[claim('mechanism1','새 실행에서는 기록한 완료 상태와 결과를 함께 확인합니다.'),
                       claim('mechanism2','완료 결과를 보존한 뒤 실패한 단계부터 이어갑니다. 저장 상태를 확인하기 전에 같은 작업을 다시 만들면 중복될 수 있습니다.')],
            example=dict(example_id='example',kind='pedagogical_illustration',input='1단계 성공, 2단계 실패',
                expected_output='1단계 결과를 보존하고 2단계를 재개',
                interpretation=claim('interpretation','성공 결과를 다시 생성하지 않는 점과, 실패 작업을 계속 진행하는 점을 구분해 읽으세요.')),
            misconception=claim('misconception','실패했다는 이유로 모든 단계를 다시 실행하는 것은 완료 결과를 재사용하는 흐름과 다릅니다.'),
            material_reading=claim('material','코드에서는 결과 저장과 상태 확인이 어느 순서인지 앞 설명과 대조해 읽으세요.'),
            covered_unit_ids=[u['unit_id'] for u in context['units']])],
        exercises=[dict(question_id='question',question='1단계가 성공하고 2단계가 실패했다면 왜 1단계 결과를 보존해야 하나요?',
            answer=claim('answer','성공 결과를 재사용하면 이미 끝난 단계를 다시 처리하지 않고 남은 작업을 이어갈 수 있기 때문입니다.'))]))


@pytest.mark.parametrize('field',['definition','purpose','mechanism','example','material_reading'])
def test_short_summary_cannot_pass_instructional_schema(inputs,tmp_path,field):
    paths,a=inputs;files=load_review_files(*paths,[],tmp_path/'unused.json')
    draft=teaching(units_for(files,a.spec.section_id,[])).model_dump()
    del draft['teaching_units'][0][field]
    with pytest.raises(ValidationError):InstructionalDraft.model_validate(draft)


def test_coverage_must_be_in_main_teaching_not_only_an_exercise(inputs,tmp_path):
    paths,a=inputs;files=load_review_files(*paths,[],tmp_path/'unused.json');context=units_for(files,a.spec.section_id,[])
    draft=teaching(context)
    assert not verify_instructional(context,draft)
    draft.teaching_units[0].covered_unit_ids.pop()
    assert verify_instructional(context,draft)==['incomplete_main_teaching_coverage']
    draft=teaching(context);draft.teaching_units[0].definition.citations[0].quote='not in source'
    assert 'unsupported_citation' in verify_instructional(context,draft)


def test_evidence_in_material_reading_stays_visible_without_table_or_code(inputs,tmp_path):
    paths,a=inputs;files=load_review_files(*paths,[],tmp_path/'unused.json');context=units_for(files,a.spec.section_id,[])
    context=copy.deepcopy(context)
    context['units'].append(dict(unit_id='authored:material-guidance',
        text='표가 없는 절도 원문 대조 설명을 본문에 포함합니다.',sources=[dict(source=dict(page=1))]))
    draft=teaching(context);guidance=draft.teaching_units[0].material_reading
    assert len(context['units'])>1
    from pdf_notion_mvp.instructional_lesson import unit_claims
    for claim in unit_claims(draft.teaching_units[0]):
        if claim is not guidance:claim.citations=claim.citations[:1]
    guidance.text='이 원문 설명은 표와 코드가 없는 경우에도 본문에서 읽을 수 있어야 합니다.'
    assert not verify_instructional(context,draft)
    notion=render_teaching(draft,context)
    html=render_instructional_html(draft,context,'합성',{'fragments':[],'blocks':[],'original_pages':[]}).decode()
    assert guidance.text in notion and guidance.text in html


def test_new_session_default_is_instructional_and_legacy_replay_is_explicit(inputs,tmp_path):
    paths,a=inputs;outline=json.loads(paths[1].read_text())
    for n in outline['nodes']:n['review_status']='confirmed'
    paths[1].write_text(json.dumps(outline))
    session,_=prepare_session(paths,a.spec.section_id,'재개',tmp_path/'new-session',
        tmp_path/'ledger/budget.sqlite',ROOT,now=NOW)
    assert session.proposal.spec.lesson_format=='instructional_v1'
    assert not session.proposal.user_approved
    assert not (tmp_path/'ledger/budget.sqlite').exists()
    schema=wire_schema('instructional_v1')
    assert 'teaching_units' in schema['properties'] and 'topics' not in schema['properties']
    assert 'summary_v1' not in a.spec.model_dump_json()  # historical fingerprints


@pytest.mark.parametrize('invalid',['format','example_kind','mechanism_count'])
def test_provider_200_invalid_instructional_response_is_preserved_without_retry(inputs,tmp_path,invalid):
    paths,legacy=inputs
    files=load_review_files(*paths,[],tmp_path/'unused.json')
    context=units_for(files,legacy.spec.section_id,[])
    draft=teaching(context).model_dump()
    if invalid=='format':draft['lesson_format']='Instructional Chapter'
    elif invalid=='example_kind':draft['teaching_units'][0]['example']['kind']='pedagogical illustration'
    else:draft['teaching_units'][0]['mechanism']=draft['teaching_units'][0]['mechanism'][:1]
    result_dir=tmp_path/'live-like-result';budget=tmp_path/'live-like-budget.sqlite';seen=[]
    proposal=create_proposal(paths,legacy.spec.section_id,output_dir=result_dir,
        budget_ledger=budget,key_project_root=ROOT,now=NOW)
    for k in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:
        setattr(proposal,k,True)
    def build(**kwargs):
        from langchain_google_genai import ChatGoogleGenerativeAI
        def respond(request):
            seen.append(request)
            schema=json.loads(request.content)['generationConfig']['responseJsonSchema']
            assert schema['properties']['lesson_format']['enum']==['instructional_v1']
            assert schema['$defs']['WorkedExample']['properties']['kind']['enum']==['pedagogical_illustration']
            assert 'const' not in schema['properties']['lesson_format']
            return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[
                {'text':json.dumps(draft,ensure_ascii=False)}]},'finishReason':'STOP'}]})
        kwargs['client_args']['transport']=httpx.MockTransport(respond)
        return ChatGoogleGenerativeAI(**kwargs)
    with pytest.raises(ValidationError):
        execute(proposal,proposal.plan_sha256,budget,result_dir,ROOT,
            client_factory=build,key_provider=lambda:'fabricated-private-key',now=NOW)
    receipts=list(result_dir.glob('*-provider.json'))
    assert len(seen)==len(receipts)==1  # one actual SDK request
    original=receipts[0].read_bytes()
    assert original  # preserve the provider receipt even when local parsing fails
    with pytest.raises(ValueError,match='no automatic retry'):
        execute(proposal,proposal.plan_sha256,budget,result_dir,ROOT,
            key_provider=lambda:pytest.fail('retry read key'),now=NOW)
    assert receipts[0].read_bytes()==original and len(seen)==1


def test_changed_lesson_format_blocks_before_key_or_budget_write(inputs,tmp_path):
    paths,_=inputs
    proposal=create_proposal(paths,'unit.part',output_dir=tmp_path/'new-result',
        budget_ledger=tmp_path/'new-ledger/budget.sqlite',key_project_root=ROOT,now=NOW)
    for k in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(proposal,k,True)
    proposal.spec.lesson_format='summary_v1'
    proposal.plan_sha256=fingerprint(proposal.spec.model_dump(mode='json'))
    with pytest.raises(ValueError,match='approved request changed'):
        execute(proposal,proposal.plan_sha256,Path(proposal.spec.budget_ledger),Path(proposal.spec.output_dir),ROOT,
            key_provider=lambda:pytest.fail('unapproved key read'),now=NOW)
    assert not Path(proposal.spec.budget_ledger).exists()


def test_folded_originals_and_native_table_spacing_preserve_structure():
    binding=UploadedPageImage(page=1,sha256='a'*64,file_upload_id=PAGE,filename='original.png')
    expected='<details>\n<summary>원본</summary>\n\t![원본 p1](file-upload://'+str(PAGE)+')\n\t<table>\n\t<tr>\n\t<td>설정</td>\n\t</tr>\n\t</table>\n</details>'
    actual=expected.replace('file-upload://'+str(PAGE),'https://private.s3.amazonaws.com/original.png?fresh=1')
    actual=actual.replace('\t<tr>','<tr>').replace('\t<td>','<td>').replace('\t</tr>','</tr>')
    assert canonical(expected,[binding])==canonical(actual,[binding],native_code_payload=True)
    moved=actual.replace('\t<table>','<table>')
    assert canonical(expected,[binding])!=canonical(moved,[binding],native_code_payload=True)


@pytest.mark.parametrize('repair_ids',[False,True])
def test_instructional_sdk_review_publication_and_repeat(reviewed,tmp_path,repair_ids):
    args,legacy,legacy_seen=reviewed
    files=load_review_files(*map(Path,legacy['input_paths']),[],tmp_path/'unused.json')
    context=units_for(files,legacy['context']['section_id'],[]);draft=teaching(context);seen=[]
    if repair_ids:draft.teaching_units[0].purpose.claim_id=draft.teaching_units[0].definition.claim_id
    proposal=create_proposal(legacy['input_paths'],context['section_id'],output_dir=tmp_path/'teaching-result',
        budget_ledger=args[2],key_project_root=ROOT,now=NOW)
    assert proposal.spec.lesson_format=='instructional_v1'
    for k in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(proposal,k,True)
    def build(**kwargs):
        from langchain_google_genai import ChatGoogleGenerativeAI
        def respond(request):
            seen.append(request)
            return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[
                {'text':draft.model_dump_json()}]},'finishReason':'STOP'}]})
        kwargs['client_args']['transport']=httpx.MockTransport(respond)
        return ChatGoogleGenerativeAI(**kwargs)
    result,status=execute(proposal,proposal.plan_sha256,args[2],tmp_path/'teaching-result',ROOT,
        client_factory=build,key_provider=lambda:'fabricated-private-key',now=NOW)
    assert status=='written' and result['lesson_format']=='instructional_v1'
    result_path=tmp_path/'teaching-result'/(result['operation_key']+'.json')
    if repair_ids:
        assert result['errors']==['duplicate_instructional_id']
        original_bytes=result_path.read_bytes()
        with pytest.raises(ValueError,match='no automatic retry'):
            execute(proposal,proposal.plan_sha256,args[2],tmp_path/'teaching-result',ROOT,
                key_provider=lambda:pytest.fail('failed-run retry key'),now=NOW)
        result,status=recover_positional_ids(result_path,budget_path=args[2])
        assert status=='written' and result_path.read_bytes()==original_bytes
        assert result['id_recovery']['text_and_citations_changed'] is False
        assert recover_positional_ids(result_path,budget_path=args[2])==(result,'unchanged')
        draft=parse_draft(result['draft']);result_path=result_path.with_name(result['operation_key']+'-ids.json')
    assert result['status']=='ready_for_content_review' and len(seen)==1
    assert json.loads(seen[0].content)==expected_request(context,'instructional_v1')
    assert not result['images_transmitted']
    assert execute(proposal,proposal.plan_sha256,args[2],tmp_path/'teaching-result',ROOT,
        key_provider=lambda:pytest.fail('repeat key lookup'),now=NOW)==(result,'unchanged')
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=review_ids(draft),reviewer='independent synthetic fixture',
        decision='accepted',notes=['이 합성 자료는 코드 실행 능력을 검증한 결과가 아닙니다.'])
    args[0]=result_path;args[1]=tmp_path/'teaching-review/review.json';args[4]=tmp_path/'teaching-reading'
    args[1].parent.mkdir()
    args[1].write_text(review.model_dump_json())
    with pytest.raises(ValueError,match='pedagogy'):prepare_lesson(*args)
    review.pedagogy_checks=sorted(PEDAGOGY_CHECKS);args[1].write_text(review.model_dump_json())
    action,cp,bindings,final=prepare_lesson(*args)
    body=action['pages'][0]['content']
    assert '학습용 가상 예시' in body and '### 헷갈리기 쉬운 점' in body
    assert body.index(draft.teaching_units[0].material_reading.text)<body.index('```python')<body.index('원본과 전사')<body.index('스스로 설명해 보기')
    assert body.count('![원본 p')==2 and '```json' not in body and '<summary>원문 근거' not in body
    assert '성공 결과를 재사용하면' in (final/'index.html').read_text()
    assert review.notes[0] in body and review.notes[0] in (final/'index.html').read_text()
    cp=cp.model_copy(update={'page_id':PAGE});fetched=remote(cp,bindings)
    repeated=prepare_lesson(*args,checkpoint=cp,remote_packet=fetched)
    assert repeated[0] is None and repeated[1].status=='confirmed' and len(seen)==1
    fetched=copy.deepcopy(fetched);fetched['text']=fetched['text'].replace('authored-p2.png','wrong.png')
    with pytest.raises(ValueError):prepare_lesson(*args,checkpoint=cp,remote_packet=fetched)
