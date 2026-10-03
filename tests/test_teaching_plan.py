import copy
import json
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.teaching_plan import (TeachingPlan,attach_plan,bind_prose,prose_slots,validate_plan)
from pdf_notion_mvp.lesson_generation import (create_proposal,execute,expected_request,fingerprint,units_for,
    ContentReview,draft_review_ids,write_reading_bundle)
from pdf_notion_mvp.instructional_lesson import PEDAGOGY_CHECKS,verify_instructional,render_teaching
from pdf_notion_mvp.rag_files import load_review_files
from test_lesson_generation import inputs,NOW,ROOT


def planned(context):
    ids=[u['unit_id'] for u in context['units']]
    def slot(name,refs=None):return dict(slot_id=name,goal='재개할 때 어떤 결과를 보존하고 왜 확인해야 하는지 설명합니다.',evidence_ids=refs or ids)
    unit=dict(teaching_id='resume',title='완료한 단계와 재개할 단계',definition=slot('definition'),
        purpose=slot('purpose'),mechanism=[slot('saved'),slot('resume-step',['supplement:async'])],
        example_input=slot('input'),example_output=slot('output'),example_interpretation=slot('interpretation'),
        misconception=slot('misconception'),material_reading=slot('materials'))
    return dict(version='reviewed_prose_plan_v1',context_digest=fingerprint(context),reviewer='independent authored fixture',
        decision='accepted',title='작업 재개',introduction='합성 원문을 대조하는 학습 흐름입니다.',objectives=['완료 결과 보존 이유를 설명합니다.'],
        units=[unit],exercises=[dict(question=slot('question'),answer=slot('answer'))],
        supplements=[dict(unit_id='supplement:async',title='공식 문서 보충',
            url='https://docs.langchain.com/oss/python/langchain/models',checked_on='2026-10-03',
            text='이것은 공식 문서와 대조한 전사라는 역할만 시험하는 합성 텍스트입니다.')])


def response(context):
    return {s.slot_id:'저장된 결과를 먼저 확인합니다. 완료한 단계는 보존하고 남은 단계를 이어갑니다.' for s in prose_slots(TeachingPlan.model_validate(context['teaching_plan']))}


@pytest.mark.parametrize('change',['source','missing_main','unknown_ref','duplicate_id','example_id_collision','unofficial_url','supp_as_pdf','blank_title','long_intro','long_unit_id','blank_supplement','long_evidence'])
def test_plan_requires_reviewed_source_binding_and_complete_main_coverage(inputs,tmp_path,change):
    paths,a=inputs;context=units_for(load_review_files(*paths,[],tmp_path/'unused'),a.spec.section_id,[])
    plan=planned(context)
    if change=='source':plan['context_digest']='wrong'
    elif change=='missing_main':
        for s in [plan['units'][0]['definition'],plan['units'][0]['purpose'],*plan['units'][0]['mechanism'],
                  plan['units'][0]['example_interpretation'],plan['units'][0]['misconception'],plan['units'][0]['material_reading']]:
            s['evidence_ids']=['supplement:async']
    elif change=='unknown_ref':plan['units'][0]['definition']['evidence_ids']=['unknown']
    elif change=='duplicate_id':plan['units'][0]['purpose']['slot_id']='definition'
    elif change=='example_id_collision':plan['exercises'][0]['question']['slot_id']='resume-example'
    elif change=='unofficial_url':plan['supplements'][0]['url']='https://example.invalid/fake'
    elif change=='blank_title':plan['title']=''
    elif change=='long_intro':plan['introduction']='가'*3001
    elif change=='long_unit_id':plan['units'][0]['teaching_id']='u'*80
    elif change=='blank_supplement':plan['supplements'][0]['text']=' '
    elif change=='long_evidence':plan['supplements'][0]['text']='가'*2001
    else:plan['supplements'][0]['unit_id']=context['units'][0]['unit_id']
    with pytest.raises(ValueError):validate_plan(plan,context)


def test_binding_preserves_generated_text_and_copies_exact_evidence_without_semantic_acceptance(inputs,tmp_path):
    paths,a=inputs;source=units_for(load_review_files(*paths,[],tmp_path/'unused'),a.spec.section_id,[])
    context=attach_plan(source,planned(source));text=response(context);draft=bind_prose(context,text)
    assert not verify_instructional(context,draft)
    assert draft.teaching_units[0].definition.text==text['definition']
    assert draft.teaching_units[0].definition.citations[0].quote==source['units'][0]['text']
    assert draft.teaching_units[0].mechanism[1].citations[0].unit_id=='supplement:async'
    assert '공식 문서로 보충한 설명' in render_teaching(draft,context)
    assert '보충 출처:' in render_teaching(draft,context)
    for change in [dict(text,definition=''),dict(text,extra='unexpected')]:
        with pytest.raises(ValueError):bind_prose(context,change)
    # A fabricated statement still requires semantic review; binding cannot bless it.
    text['definition']='모든 모델 교체는 응답 정확도를 보장합니다.'
    assert bind_prose(context,text).teaching_units[0].definition.text==text['definition']


def test_prose_sdk_receipt_review_render_repeat_and_plan_tamper(inputs,tmp_path):
    paths,legacy=inputs;files=load_review_files(*paths,[],tmp_path/'unused')
    base=units_for(files,legacy.spec.section_id,[]);context=attach_plan(base,planned(base));seen=[]
    plan_path=tmp_path/'plan.json';plan_path.write_text(json.dumps(planned(base)))
    result_dir=tmp_path/'results';budget=tmp_path/'paid.sqlite'
    approval=create_proposal(paths,legacy.spec.section_id,output_dir=result_dir,budget_ledger=budget,
        key_project_root=ROOT,now=NOW,teaching_plan_path=plan_path)
    for k in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(approval,k,True)
    def build(**kwargs):
        from langchain_google_genai import ChatGoogleGenerativeAI
        def reply(request):
            seen.append(request);assert json.loads(request.content)==expected_request(context,'instructional_v1')
            return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[
                {'text':json.dumps(response(context))}]},'finishReason':'STOP'}]})
        kwargs['client_args']['transport']=httpx.MockTransport(reply)
        return ChatGoogleGenerativeAI(**kwargs)
    result,status=execute(approval,approval.plan_sha256,budget,result_dir,ROOT,
        client_factory=build,key_provider=lambda:'fabricated-private-key',now=NOW)
    assert status=='written' and len(seen)==1 and result['status']=='ready_for_content_review'
    assert result['semantic_correctness_verified'] is False
    rp=result_dir/(result['operation_key']+'.json')
    assert execute(approval,approval.plan_sha256,budget,result_dir,ROOT,now=NOW,
        key_provider=lambda:pytest.fail('repeat key'))==(result,'unchanged')
    draft=bind_prose(context,response(context))
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=draft_review_ids(draft),
        reviewer='independent synthetic fixture',decision='accepted',notes=[],pedagogy_checks=sorted(PEDAGOGY_CHECKS))
    review_path=tmp_path/'review/content.json';review_path.parent.mkdir();review_path.write_text(review.model_dump_json())
    folder,status=write_reading_bundle(rp,review_path,files,legacy.spec.section_id,'재개',tmp_path/'reading',budget_path=budget)
    assert '공식 문서로 보충한 설명' in (folder/'index.html').read_text() and status=='written'
    changed=planned(base);changed['units'][0]['purpose']['goal']='Changed after approval'
    plan_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='plan changed'):
        execute(approval,approval.plan_sha256,budget,result_dir,ROOT,now=NOW,
            key_provider=lambda:pytest.fail('tampered plan key'))
    with pytest.raises(ValueError,match='plan changed'):
        write_reading_bundle(rp,review_path,files,legacy.spec.section_id,'재개',tmp_path/'reading',budget_path=budget)
