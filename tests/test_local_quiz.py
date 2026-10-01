import hashlib
import json
from pathlib import Path

import pytest

from pdf_notion_mvp.contracts import FixtureInput
from pdf_notion_mvp.local_quiz import plan_local_quiz
from pdf_notion_mvp.quiz import cloze_question, prepare_context, verify_quiz
from pdf_notion_mvp.quiz_files_cli import main
from pdf_notion_mvp.rag_cli import load_fixture
from pdf_notion_mvp.review import HierarchicalOutline, ReviewLayer, document_digest

ROOT=Path(__file__).parents[1]


@pytest.fixture
def lesson(): return load_fixture(ROOT)


def plan(f,**kwargs): return plan_local_quiz(f.source,f.hierarchy,f.review,'rag.control',**kwargs)


def save(root,f):
    root.mkdir(parents=True,exist_ok=True)
    for name,value in [('source',f.source),('outline',f.hierarchy),('review',f.review)]:
        (root/f'{name}.json').write_text(value.model_dump_json())
    return ['--source',str(root/'source.json'),'--outline',str(root/'outline.json'),'--review',str(root/'review.json'),'--section','rag.control']


def run_cli(root,args):
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    return exc.value.code


def test_deterministic_local_rule_plan_is_neutral_and_source_preserving(lesson):
    before=lesson.model_dump_json()
    first=plan(lesson,max_questions=3)
    second=plan(lesson,max_questions=3)
    assert first==second and first.status=='ready_for_review'
    assert first.generation_mode=='deterministic_cloze_rule' and first.provider is None and first.target is None
    assert first.human_review_required and not first.actual_model_generation and not first.semantic_correctness_verified
    assert len(first.questions)==2 and first.source_pages==2 and first.source_blocks==10
    assert lesson.model_dump_json()==before
    assert first.events==['prepare_confirmed_context','local_rule_generate','independent_validate']
    context=prepare_context(lesson.source,lesson.hierarchy,lesson.review,'rag.control')
    from pdf_notion_mvp.quiz import QuizBatch
    assert verify_quiz(context,QuizBatch(questions=[q.question for q in first.questions]),3)==[]
    assert all(q.question.question==cloze_question(q.question.source_quote,q.question.answer) for q in first.questions)
    correction=first.questions[1]
    assert correction.original_text==lesson.review.corrections[0].original_text
    assert correction.effective_text==lesson.review.corrections[0].proposed_text
    assert correction.question.source_quote==correction.effective_text
    assert correction.correction_id=='rag-fix' and correction.source.page==1
    assert all(q.question.source_block_ids[0] not in {'rag-candidate','rag-budget','rag-version'} for q in first.questions)


@pytest.mark.parametrize('change',['claim','answer','quote','candidate','foreign_section','duplicate','context_mutation'])
def test_independent_verifier_blocks_tampered_rule_output(lesson,monkeypatch,change):
    from pdf_notion_mvp import local_quiz
    original=local_quiz.rule_batch
    def changed(context,limit):
        batch=original(context,limit)
        q=batch.questions[0]
        if change=='claim': q.explanation='출처에 없는 발명한 설명'
        if change=='answer': q.answer='invented-answer'
        if change=='quote': q.source_quote='invented quote'
        if change=='candidate': q.source_block_ids=['rag-candidate']
        if change=='foreign_section': q.source_block_ids=['rag-budget']
        if change=='duplicate': batch.questions.append(q.model_copy(deep=True))
        if change=='context_mutation':
            context.evidence[0].text='발명한 수치 999'
            q.source_quote=context.evidence[0].text
            q.answer='999'
            q.question=cloze_question(q.source_quote,q.answer)
            q.explanation='근거 원문: '+q.source_quote
        return batch
    monkeypatch.setattr(local_quiz,'rule_batch',changed)
    before=lesson.model_dump_json()
    result=plan(lesson)
    assert result.status=='failed_human_review' and not result.questions and result.errors
    assert lesson.model_dump_json()==before


@pytest.mark.parametrize('section',['missing','rag-unit',''])
def test_unknown_or_parent_section_cannot_generate(lesson,section):
    with pytest.raises(ValueError): plan_local_quiz(lesson.source,lesson.hierarchy,lesson.review,section)


@pytest.mark.parametrize('limit',[0,4,True,'1'])
def test_question_limit_is_bounded(lesson,limit):
    with pytest.raises(ValueError): plan(lesson,max_questions=limit)


@pytest.mark.parametrize('text',['Model','123 456','기존 [빈칸] 표식은 사용 금지','긴 문장 '*600])
def test_ineligible_text_does_not_fabricate_question(lesson,text):
    lesson.source.document.blocks[1].text=text
    lesson.review.source_digest=document_digest(lesson.source.document)
    lesson.review.corrections[0].status='candidate'
    result=plan(lesson)
    assert result.status=='failed_human_review' and result.questions==[]
    assert 'question_count' in result.errors


def test_duplicate_evidence_does_not_create_duplicate_questions(lesson):
    lesson.source.document.blocks[1].text=lesson.review.corrections[0].proposed_text
    lesson.review.source_digest=document_digest(lesson.source.document)
    result=plan(lesson,max_questions=3)
    assert result.status=='ready_for_review' and len(result.questions)==1


def test_changed_source_or_review_changes_plan_identity(lesson):
    first=plan(lesson)
    lesson.review.corrections[0].basis+=' 검토 기록 추가'
    second=plan(lesson)
    assert first.review_digest!=second.review_digest
    assert first.questions[0].operation_key!=second.questions[0].operation_key


def test_file_cli_returns_local_rules_only_and_never_reads_keys(lesson,tmp_path,monkeypatch):
    from pdf_notion_mvp import providers
    def no_keys(*args,**kwargs): pytest.fail('key/provider access is forbidden')
    monkeypatch.setattr(providers,'selected_key',no_keys)
    monkeypatch.setattr(providers,'build_provider',no_keys)
    old_read=Path.read_text
    def guarded(p,*a,**k):
        assert not p.name.startswith('.env')
        return old_read(p,*a,**k)
    monkeypatch.setattr(Path,'read_text',guarded)
    args=save(tmp_path/'inputs',lesson)
    before={p:p.read_bytes() for p in (tmp_path/'inputs').iterdir()}
    assert run_cli(tmp_path,args)==0
    output=tmp_path/'output/local-rule-quiz.json'
    first=output.read_bytes()
    assert run_cli(tmp_path,args)==0 and output.read_bytes()==first
    data=json.loads(first)
    assert data['plan']['generation_mode']=='deterministic_cloze_rule' and data['plan']['provider'] is None
    assert data['actual_key_reads']==data['actual_model_requests']==data['notion_requests']==0
    assert all(p.read_bytes()==before[p] for p in before)


@pytest.mark.parametrize('damage',['missing','json','digest','outside_section','candidate_only'])
def test_invalid_or_unconfirmed_inputs_do_not_produce_ready_questions(lesson,tmp_path,damage):
    if damage=='candidate_only':
        lesson.review.corrections[0].status='candidate'
        for correction in lesson.review.corrections: correction.status='candidate'
        from pdf_notion_mvp.review import Correction
        b=lesson.source.document.blocks[1]
        lesson.review.corrections.append(Correction(correction_id='candidate-checkpoint',block_id=b.block_id,original_text=b.text,source=b.source,
            proposed_text=b.text,status='candidate',basis='합성 후보',reviewer='authored-reviewer',evidence_block_id='rag-visual',evidence_bbox=b.source.bbox))
    args=save(tmp_path/'inputs',lesson)
    if damage=='missing': (tmp_path/'inputs/review.json').unlink()
    if damage=='json': (tmp_path/'inputs/review.json').write_text('{broken')
    if damage=='digest':
        path=tmp_path/'inputs/review.json';data=json.loads(path.read_text());data['source_digest']='0'*64;path.write_text(json.dumps(data))
    if damage=='outside_section':
        path=tmp_path/'inputs/source.json';data=json.loads(path.read_text());data['document']['blocks'][-1]['source']['bbox']['x1']=999;path.write_text(json.dumps(data))
    assert run_cli(tmp_path,args)==1
    assert not (tmp_path/'output/local-rule-quiz.json').exists()


@pytest.mark.parametrize('target',['source','outline','review'])
def test_output_alias_of_input_is_blocked(lesson,tmp_path,target):
    args=save(tmp_path/'output',lesson)
    output=tmp_path/'output'/f'{target}.json'
    before=output.read_bytes()
    assert run_cli(tmp_path,args+['--output',str(output)])==1
    assert output.read_bytes()==before


@pytest.mark.parametrize('failure',['partial_write','replace'])
def test_atomic_save_failure_keeps_previous_review_plan(lesson,tmp_path,monkeypatch,capsys,failure):
    from pdf_notion_mvp import quiz_files_cli
    args=save(tmp_path/'inputs',lesson)
    assert run_cli(tmp_path,args)==0
    output=tmp_path/'output/local-rule-quiz.json'
    before=output.read_bytes()
    def partial(value,stream,*a,**k):
        stream.write('{broken')
        stream.flush()
        raise OSError('fabricated-secret-not-for-logs')
    def fail_replace(p,target): raise OSError('fabricated-secret-not-for-logs')
    with monkeypatch.context() as scoped:
        if failure=='partial_write': scoped.setattr(quiz_files_cli.json,'dump',partial)
        else: scoped.setattr(Path,'replace',fail_replace)
        assert run_cli(tmp_path,args)==1
    assert output.read_bytes()==before and not list(output.parent.glob('.local-rule-quiz.json-*'))
    assert run_cli(tmp_path,args)==0
    assert 'fabricated-secret' not in capsys.readouterr().out


def test_ocr_shaped_files_only_use_confirmed_transcription(tmp_path):
    # Authored mock raster/IR: no OCR engine or image decoding is executed.
    raster=tmp_path/'raster.dat';payload=b'authored-mock-raster';raster.write_bytes(payload)
    def provenance(y): return {'document_id':'authored-quiz-ocr','version':'mock-v1','page':1,'method':'ocr','confidence':0.7,'bbox':{'x0':40,'y0':y,'x1':550,'y1':y+20}}
    body={'block_id':'confirmed','kind':'text','text':'원본 오탈자','source':provenance(60)}
    candidate={'block_id':'candidate','kind':'text','text':'미확정 후보 원문','source':provenance(100)}
    raw={'block_id':'raw','kind':'text','text':'무교정 텍스트는 제외합니다.','source':provenance(140)}
    source=FixtureInput.model_validate({'kind':'ocr_ir','document':{'document_id':'authored-quiz-ocr','version':'mock-v1','name':'작성한 OCR 모양 자료','pages':[{'number':1,'width':600,'height':800}],'blocks':[body,candidate,raw,{'block_id':'raster','kind':'image','asset_ref':str(raster),'sha256':hashlib.sha256(payload).hexdigest(),'source':{'document_id':'authored-quiz-ocr','version':'mock-v1','page':1,'method':'raster','bbox':{'x0':0,'y0':0,'x1':600,'y1':800}}}],'extraction':{'engine':'authored-mock-ocr','pages_processed':[1],'human_review_required':True}}})
    outline=HierarchicalOutline.model_validate({'document_id':source.document.document_id,'version':source.document.version,'nodes':[{'node_id':'only','title':'작성한 절','source_pages':[1],'block_ids':['confirmed','candidate','raw','raster']}]})
    review=ReviewLayer.model_validate({'document_id':source.document.document_id,'version':source.document.version,'source_digest':document_digest(source.document),'corrections':[
        {'correction_id':'fix-'+b['block_id'],'block_id':b['block_id'],'original_text':b['text'],'source':b['source'],'proposed_text':text,'status':status,'basis':'합성 근거','reviewer':'authored-reviewer','evidence_block_id':'raster','evidence_bbox':b['source']['bbox']}
        for b,status,text in [(body,'confirmed','확정 문장은 원문 출처를 보존합니다.'),(candidate,'candidate','미확정 비행 후보는 제외합니다.')]]})
    first=plan_local_quiz(source,outline,review,'only',asset_root=tmp_path,max_questions=3)
    assert first.status=='ready_for_review' and len(first.questions)==1
    assert first.questions[0].question.source_block_ids==['confirmed']
    assert first.questions[0].evidence_layer=='confirmed_transcription' and first.questions[0].source.method=='ocr'
    raster.write_bytes(b'changed')
    with pytest.raises(ValueError): plan_local_quiz(source,outline,review,'only',asset_root=tmp_path)
