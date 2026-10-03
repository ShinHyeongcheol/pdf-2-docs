import copy
import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pdf_notion_mvp.lesson_generation import (COST_MICRO_USD,ContentReview,LessonDraft,
    create_proposal,execute,expected_request,fingerprint,operation_key,units_for,verify_draft,
    write_reading_bundle,_budget_connection,seed_previous_run,wire_schema,safe_error_code,recover_positional_ids)
from pdf_notion_mvp.live_run import CompletedRunReceipt
from pdf_notion_mvp.rag_files import load_review_files

NOW=datetime(2026,10,2,tzinfo=timezone.utc)
ROOT=Path(__file__).parents[1]

@pytest.fixture
def inputs(tmp_path):
    directory=tmp_path/'inputs'; directory.mkdir()
    names=['synthetic.json','synthetic-outline.json','synthetic-review.json']
    paths=[]
    for name in names:
        p=directory/name; p.write_bytes((ROOT/'fixtures'/name).read_bytes()); paths.append(p)
    a=create_proposal(paths,'unit.part',lesson_format='summary_v1',output_dir=tmp_path/'results',budget_ledger=tmp_path/'ledger/budget.sqlite',key_project_root=ROOT,now=NOW)
    for name in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:
        setattr(a,name,True)
    return paths,a


def draft_for(context):
    u=next(u for u in context['units'] if '체크포인트' in u['text'])
    quote=u['text']
    citation={'unit_id':u['unit_id'],'quote':quote}
    topics=[{'title':'합성 검토 주제 '+str(i),'claims':[{'claim_id':'c'+str(i),'text':'성공한 단계는 보존한다는 합성 자료입니다.','citations':[citation]}]} for i in range(3)]
    exercises=[]
    for i,answer in enumerate(['성공한','단계','체크포인트']):
        exercises.append({'question_id':'q'+str(i),'kind':'cloze',
            'question':'자료의 빈칸을 채우세요: '+quote.replace(answer,'[빈칸]',1),'answer':answer,
            'explanation':{'claim_id':'e'+str(i),'text':'합성 원문에서 답을 찾습니다.','citations':[citation]},
            'source_quote':quote,'unit_id':u['unit_id']})
    return LessonDraft.model_validate({'topics':topics,'exercises':exercises})


def factory(context,seen,violation=None):
    from langchain_google_genai import ChatGoogleGenerativeAI
    def respond(request):
        seen.append(request)
        if isinstance(violation,dict):
            return httpx.Response(violation['http_status'],json=violation['packet'])
        if violation=='timeout': raise httpx.ReadTimeout('fabricated-private-key',request=request)
        data=draft_for(context).model_dump(mode='json')
        if violation=='duplicate_ids': data['exercises'][0]['explanation']['claim_id']=data['topics'][0]['claims'][0]['claim_id']
        if violation=='bounds': data['topics']=[]
        if violation=='schema_error': return httpx.Response(400,json={'error':{'code':400,'status':'INVALID_ARGUMENT','message':'response schema exceeds complexity; fabricated-private-key'}})
        if violation=='malformed': return httpx.Response(200,content=b'not valid JSON',headers={'content-type':'application/json'})
        if violation in {'key_echo','escaped_key_echo'}:
            text=json.dumps({'echo':'fabricated-private-key'})
            if violation=='escaped_key_echo':text=text.replace('fabricated',r'\u0066abricated')
            return httpx.Response(200,content=text.encode(),headers={'content-type':'application/json'})
        if violation=='citation': data['topics'][0]['claims'][0]['citations'][0]['quote']='없는 근거'
        return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[{'text':json.dumps(data,ensure_ascii=False)}]},'finishReason':'STOP'}],
            'usageMetadata':{'promptTokenCount':12,'candidatesTokenCount':120,'totalTokenCount':132}})
    def build(**kwargs):
        args=dict(kwargs['client_args']); args['transport']=httpx.MockTransport(respond)
        if violation=='request':
            def attack(r):
                d=json.loads(r.content); d['contents'][0]['parts'][0]['text']+='unapproved'; r._content=json.dumps(d).encode()
            args['event_hooks']['request']=[attack]+args['event_hooks']['request']
        kwargs['client_args']=args
        return ChatGoogleGenerativeAI(**kwargs)
    return build


def run(tmp_path,a,seen,violation=None):
    paths=[Path(p) for p in a.spec.input_paths]
    files=load_review_files(*paths,[],tmp_path/'unused.json')
    ctx=units_for(files,a.spec.section_id,a.spec.exclude_pages)
    return execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,
        client_factory=factory(ctx,seen,violation),key_provider=lambda:'fabricated-private-key',now=NOW)


def test_real_sdk_serialization_usage_and_saved_rerun_no_second_key_lookup(inputs,tmp_path):
    _,a=inputs; seen=[]
    result,status=run(tmp_path,a,seen)
    assert status=='written' and result['status']=='ready_for_content_review'
    assert result['usage_tokens']=={'input_tokens':12,'output_tokens':120,'total_tokens':132}
    assert result['usage_estimated_usd']=='0.000183'
    assert result['request_count']==1 and result['execution_mode']=='injected' and not result['images_transmitted']
    assert json.loads(seen[0].content)==expected_request(result['context'])
    assert 'asset_ref' not in seen[0].content.decode() and str(tmp_path) not in seen[0].content.decode()
    result2,status=execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,
        client_factory=lambda **kw:pytest.fail('second client'),key_provider=lambda:pytest.fail('second key'),now=NOW)
    assert status=='unchanged' and result2==result and len(seen)==1


@pytest.mark.parametrize('gate',['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed','digest','source','expiry','snapshot'])
def test_gates_before_key_or_budget(inputs,tmp_path,gate):
    paths,a=inputs
    if gate in type(a).model_fields: setattr(a,gate,False)
    elif gate=='digest': a.plan_sha256='0'*64
    elif gate=='source': paths[0].write_bytes(paths[0].read_bytes()+b' ')
    elif gate=='expiry': a.spec.expires_at=NOW
    elif gate=='snapshot': a.spec.model_snapshot.input_usd_per_million=Decimal('0.01')
    if gate in ['expiry','snapshot']: a.plan_sha256=fingerprint(a.spec.model_dump(mode='json'))
    with pytest.raises(ValueError):
        execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,
            key_provider=lambda:pytest.fail('key read'),now=NOW)
    assert not (tmp_path/'ledger').exists()


@pytest.mark.parametrize('violation',['timeout','request','citation'])
def test_failed_attempt_consumed_and_never_retried(inputs,tmp_path,violation):
    _,a=inputs; seen=[]
    if violation=='citation':
        result,_=run(tmp_path,a,seen,violation)
        assert result['status']=='failed_content_validation' and 'unsupported_citation' in result['errors']
    else:
        with pytest.raises(Exception): run(tmp_path,a,seen,violation)
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    assert db.execute('SELECT status,request_count,cost FROM reservations').fetchone()==('failed',0 if violation=='request' else 1,COST_MICRO_USD)
    db.close()
    with pytest.raises(ValueError,match='consumed'):
        execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,
                key_provider=lambda:pytest.fail('second key'),now=NOW)


def test_budget_exhaustion_and_legacy_seed(inputs,tmp_path):
    _,a=inputs
    legacy=CompletedRunReceipt(run_id='00000000-0000-4000-8000-000000000001',plan_sha256='0'*64,completion_sha256='1'*64,
        provider='gemini',model='gemini-3.1-flash-lite',execution_mode='network_unattested',source_digest='2'*64,evidence_digest='3'*64,request_count=1)
    p=tmp_path/'legacy.json'; p.write_text(legacy.model_dump_json())
    ledger=tmp_path/'ledger/budget.sqlite'
    seed_previous_run(ledger,p); seed_previous_run(ledger,p)
    db=_budget_connection(ledger)
    assert db.execute('SELECT sum(cost),sum(request_count) FROM reservations').fetchone()==(COST_MICRO_USD,1)
    db.execute('INSERT INTO reservations(operation,run_id,cost,status) VALUES(?,?,?,?)',('other','other',10000000-COST_MICRO_USD,'reserved'))
    db.close()
    with pytest.raises(ValueError,match='budget'):
        execute(a,a.plan_sha256,ledger,tmp_path/'results',ROOT,key_provider=lambda:pytest.fail('key read'),now=NOW)


def test_existing_nonledger_database_preserved(tmp_path):
    p=tmp_path/'user.sqlite'; db=sqlite3.connect(p); db.execute('CREATE TABLE original(value TEXT)'); db.commit(); db.close()
    before=p.read_bytes()
    with pytest.raises(ValueError): _budget_connection(p)
    assert p.read_bytes()==before


def test_duplicate_parallel_operation_one_transport(inputs,tmp_path):
    _,a=inputs; seen=[]
    # Initialize ledger before simultaneous logical reservations, so this tests request deduplication.
    db=_budget_connection(tmp_path/'ledger/budget.sqlite'); db.close()
    def go(_):
        try: return run(tmp_path,a,seen)[1]
        except ValueError: return 'consumed'
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(go,[1,2]))
    assert results.count('written')==1 and len(seen)==1


def test_scope_omission_candidates_fragments_and_table_lineage(tmp_path):
    from pdf_notion_mvp.rag_cli import load_fixture
    f=load_fixture(ROOT)
    paths=[]; d=tmp_path/'input'; d.mkdir()
    for name,value in [('source',f.source),('outline',f.hierarchy),('review',f.review)]:
        p=d/(name+'.json'); p.write_text(value.model_dump_json()); paths.append(p)
    files=load_review_files(*paths,[],tmp_path/'x.json')
    units=units_for(files,'rag.control',[])
    text=json.dumps(units,ensure_ascii=False)
    assert 'rag-candidate' not in text and '초능력' not in text
    assert all(u['sources'][0]['source']['page']==1 for u in units['units'])
    with pytest.raises(ValueError): units_for(files,'rag.control',[2])
    with pytest.raises(ValueError): units_for(files,'rag.control',[1])


def test_draft_claim_question_attack_checks(inputs,tmp_path):
    paths,a=inputs; ctx=units_for(load_review_files(*paths,[],tmp_path/'x.json'),'unit.part',[])
    d=draft_for(ctx)
    assert verify_draft(ctx,d)==[]
    d.exercises[0].answer='없는 정답'; d.topics[0].claims[0].citations[0].unit_id='outside'
    assert {'unsupported_answer','unsupported_citation','invalid_cloze'}<=set(verify_draft(ctx,d))


def test_independent_content_review_required_and_escaped_output(inputs,tmp_path):
    paths,a=inputs; result,_=run(tmp_path,a,[])
    result_path=tmp_path/'results'/(operation_key(a.spec)+'.json')
    draft=LessonDraft.model_validate(result['draft'])
    ids=[c.claim_id for t in draft.topics for c in t.claims]+[q.question_id for q in draft.exercises]+[q.explanation.claim_id for q in draft.exercises]
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=ids,reviewer='independent fixture reviewer',decision='accepted',notes=['<script>authored</script>'])
    p=tmp_path/'reviews/review.json'; p.parent.mkdir(); p.write_text(review.model_dump_json())
    files=load_review_files(*paths,[],tmp_path/'x.json')
    final,status=write_reading_bundle(result_path,p,files,'unit.part','체크포인트',tmp_path/'reading',budget_path=tmp_path/'ledger/budget.sqlite')
    assert status=='written' and '&lt;script&gt;' in (final/'index.html').read_text()
    mtimes={v.name:v.stat().st_mtime_ns for v in final.iterdir()}
    assert write_reading_bundle(result_path,p,files,'unit.part','체크포인트',tmp_path/'reading',budget_path=tmp_path/'ledger/budget.sqlite')==(final,'unchanged')
    assert mtimes=={v.name:v.stat().st_mtime_ns for v in final.iterdir()}
    review.reviewed_ids=ids[:-1]; p.write_text(review.model_dump_json())
    with pytest.raises(ValueError): write_reading_bundle(result_path,p,files,'unit.part','체크포인트',tmp_path/'reading',budget_path=tmp_path/'ledger/budget.sqlite')


@pytest.mark.parametrize('changed',['output','budget','key'])
def test_approved_local_paths_cannot_be_switched(inputs,tmp_path,changed):
    _,a=inputs
    args=[tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT]
    args[{'budget':0,'output':1,'key':2}[changed]]=tmp_path/'other'
    with pytest.raises(ValueError,match='path changed'):
        execute(a,a.plan_sha256,*args,key_provider=lambda:pytest.fail('key read'),now=NOW)
    assert not (tmp_path/'ledger').exists()


def test_saved_result_edit_and_existing_output_not_overwritten(inputs,tmp_path):
    _,a=inputs; run(tmp_path,a,[])
    p=tmp_path/'results'/(operation_key(a.spec)+'.json')
    d=json.loads(p.read_text()); d['draft']['topics'][0]['title']='수정한 제목'; p.write_text(json.dumps(d))
    before=p.read_bytes()
    with pytest.raises(ValueError,match='saved result changed'):
        execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,key_provider=lambda:pytest.fail('key read'),now=NOW)
    assert p.read_bytes()==before


@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_ledger_alias_blocks_before_key(inputs,tmp_path,kind):
    _,a=inputs
    p=tmp_path/'ledger/budget.sqlite'; p.parent.mkdir()
    source=Path(a.spec.input_paths[0]); before=source.read_bytes()
    if kind=='symlink': p.symlink_to(source)
    else:
        import os
        os.link(source,p)
    with pytest.raises(ValueError):
        execute(a,a.plan_sha256,p,tmp_path/'results',ROOT,key_provider=lambda:pytest.fail('key read'),now=NOW)
    assert source.read_bytes()==before


def test_incomplete_json_one_attempt_and_cli_secret_redaction(inputs,tmp_path,capsys,monkeypatch):
    from pdf_notion_mvp import lesson_generation as module
    def bad(*args,**kwargs): raise RuntimeError('fabricated-private-key-never-print')
    monkeypatch.setattr(module,'create_proposal',bad)
    paths,a=inputs
    with pytest.raises(SystemExit) as e:
        module.main(['--source',str(paths[0]),'--outline',str(paths[1]),'--review',str(paths[2]),'--section','unit.part',
                    '--output-dir',str(tmp_path/'results'),'--budget-ledger',str(tmp_path/'ledger/budget.sqlite'),'--key-project-root',str(ROOT)])
    assert e.value.code==1 and 'fabricated-private-key' not in capsys.readouterr().err


def test_table_label_cannot_export_code_lineage(inputs,tmp_path):
    paths,_=inputs
    review=json.loads(paths[2].read_text())
    review['fragments'].append(dict(fragment_id='disguised-code',section_id='unit.part',source_block_ids=['b4'],
        kind='table',text='def double(value): return value * 2',status='confirmed',basis='adversarial authored fixture',correction_ids=[]))
    paths[2].write_text(json.dumps(review))
    with pytest.raises(ValueError,match='table evidence'):
        units_for(load_review_files(*paths,[],tmp_path/'unused.json'),'unit.part',[])


def test_block_and_fragment_namespaces_preserve_both_units(inputs,tmp_path):
    from pdf_notion_mvp.contracts import FixtureInput
    from pdf_notion_mvp.review import document_digest
    paths,_=inputs
    source,outline,review=[json.loads(p.read_text()) for p in paths]
    next(b for b in source['document']['blocks'] if b['block_id']=='b7')['block_id']='fragment:collision'
    for n in outline['nodes']: n['block_ids']=['fragment:collision' if i=='b7' else i for i in n['block_ids']]
    table_text=copy.deepcopy(source['document']['blocks'][-1])
    table_text.update(block_id='table-body',text='단계와 출력의 확정 합성 표')
    source['document']['blocks'].append(table_text)
    next(n for n in outline['nodes'] if n['node_id']=='unit.part')['block_ids'].append('table-body')
    review['source_digest']=document_digest(FixtureInput.model_validate(source).document)
    review['fragments'].append(dict(fragment_id='collision',section_id='unit.part',source_block_ids=['table-body'],kind='table',
        text='단계와 출력의 확정 합성 표',status='confirmed',basis='authored independent table',correction_ids=[]))
    for p,d in zip(paths,[source,outline,review]): p.write_text(json.dumps(d))
    context=units_for(load_review_files(*paths,[],tmp_path/'unused.json'),'unit.part',[])
    assert {'block:fragment:collision','fragment:collision'}<=set(u['unit_id'] for u in context['units'])
    assert len(context['units'])==len(set(u['unit_id'] for u in context['units']))


def test_source_excluded_candidate_cannot_change_only_in_memory(inputs,tmp_path):
    paths,a=inputs; result,_=run(tmp_path,a,[])
    result_path=tmp_path/'results'/(operation_key(a.spec)+'.json')
    draft=LessonDraft.model_validate(result['draft'])
    ids=[c.claim_id for t in draft.topics for c in t.claims]+[q.question_id for q in draft.exercises]+[q.explanation.claim_id for q in draft.exercises]
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=ids,reviewer='independent authored reviewer',decision='accepted',notes=[])
    rp=tmp_path/'content/review.json'; rp.parent.mkdir(); rp.write_text(review.model_dump_json())
    files=load_review_files(*paths,[],tmp_path/'unused.json')
    next(f for f in files.review.fragments if f.status=='candidate').text='UNAPPROVED_LOCAL_MEMORY_CHANGE'
    assert fingerprint(units_for(files,'unit.part',[]))==result['context_digest']
    with pytest.raises(ValueError,match='authoritative input'):
        write_reading_bundle(result_path,rp,files,'unit.part','체크포인트',tmp_path/'reading',budget_path=tmp_path/'ledger/budget.sqlite')


def test_wire_schema_omits_only_metadata_and_bounds_and_local_validation_remains(inputs,tmp_path):
    _,a=inputs
    serialized=json.dumps(wire_schema())
    for key in ['minLength','maxLength','minItems','maxItems']:
        assert '"'+key+'"' not in serialized
    assert LessonDraft.model_json_schema()['properties']['topics']['minItems']==1
    assert LessonDraft.model_json_schema()['properties']['topics']['maxItems']==6
    assert 'title' not in wire_schema()
    assert 'title' in wire_schema()['$defs']['Topic']['properties']
    assert wire_schema()['additionalProperties'] is False
    assert wire_schema()['required']==['topics','exercises']
    assert wire_schema()['$defs']['Exercise']['properties']['kind']['enum']==['short_answer','cloze']
    with pytest.raises(ValueError): run(tmp_path,a,[], 'bounds')
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    assert db.execute('SELECT status,request_count,error_code FROM reservations').fetchone()==('failed',1,'ValidationError')
    db.close()


@pytest.mark.parametrize('count',[2,3])
def test_source_grounded_topic_count_is_not_a_fixed_quota(inputs,tmp_path,count):
    _,a=inputs;ctx=units_for(load_review_files(*[Path(p) for p in a.spec.input_paths],[],tmp_path/'unused.json'),a.spec.section_id,[])
    draft=draft_for(ctx);draft.topics=draft.topics[:count]
    assert verify_draft(ctx,draft)==[]


def test_missing_evidence_and_duplicate_objectives_still_rejected(inputs,tmp_path):
    _,a=inputs;ctx=units_for(load_review_files(*[Path(p) for p in a.spec.input_paths],[],tmp_path/'unused.json'),a.spec.section_id,[])
    draft=draft_for(ctx);draft.topics=draft.topics[:2]
    draft.topics[1].title=draft.topics[0].title
    assert 'duplicate_topic' in verify_draft(ctx,draft)
    draft.topics[0].claims[0].citations=[]
    with pytest.raises(ValueError):verify_draft(ctx,draft)


def test_invalid_response_is_saved_before_validation_and_never_retried(inputs,tmp_path):
    _,a=inputs;seen=[]
    with pytest.raises(ValueError):run(tmp_path,a,seen,'bounds')
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    status,path,digest,count=db.execute('select status,result_path,result_sha,request_count from reservations').fetchone()
    assert (status,count)==('failed',1)
    saved=Path(path);original=saved.read_bytes();receipt=json.loads(original)
    assert fingerprint(receipt)==digest and receipt['provider_packet']['candidates'][0]['finishReason']=='STOP'
    assert json.loads(receipt['provider_packet']['candidates'][0]['content']['parts'][0]['text'])['topics']==[]
    assert saved.stat().st_mode & 0o777==0o600 and b'fabricated-private-key' not in original
    with pytest.raises(ValueError,match='consumed or ambiguous'):
        execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,client_factory=lambda **kw:pytest.fail('retry'),key_provider=lambda:pytest.fail('second key'),now=NOW)
    assert saved.read_bytes()==original and len(seen)==1
    db.close()


def test_even_malformed_http200_response_bytes_survive_sdk_failure(inputs,tmp_path):
    _,a=inputs;seen=[]
    with pytest.raises(Exception):run(tmp_path,a,seen,'malformed')
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    status,path,digest,count=db.execute('select status,result_path,result_sha,request_count from reservations').fetchone()
    receipt=json.loads(Path(path).read_text())
    assert status=='failed' and count==len(seen)==1 and fingerprint(receipt)==digest
    assert receipt['provider_packet'] is None
    assert base64.b64decode(receipt['provider_body_base64'])==b'not valid JSON'
    db.close()


@pytest.mark.parametrize('violation',['key_echo','escaped_key_echo'])
def test_provider_credential_echo_is_redacted_and_consumed(inputs,tmp_path,violation):
    _,a=inputs;seen=[]
    with pytest.raises(ValueError,match='credential-bearing'):run(tmp_path,a,seen,violation)
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    status,path,digest,count=db.execute('select status,result_path,result_sha,request_count from reservations').fetchone()
    receipt=json.loads(Path(path).read_text())
    assert status=='failed' and count==len(seen)==1 and fingerprint(receipt)==digest
    assert receipt['credential_redacted'] and receipt['provider_packet']['echo']=='[REDACTED_API_KEY]'
    assert b'fabricated-private-key' not in Path(path).read_bytes()
    assert b'fabricated-private-key' not in base64.b64decode(receipt['provider_body_base64'])
    db.close()


def test_sdk_schema_rejection_records_only_allowlisted_diagnostic(inputs,tmp_path):
    _,a=inputs; seen=[]
    with pytest.raises(Exception): run(tmp_path,a,seen,'schema_error')
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    assert db.execute('SELECT status,request_count,error_code FROM reservations').fetchone()==('failed',1,'GoogleInvalidRequestError:schema')
    db.close()
    assert b'fabricated-private-key' not in (tmp_path/'ledger/budget.sqlite').read_bytes()
    assert safe_error_code(type('ProviderSecretClass', (Exception,), {})('private'))=='OtherError'


@pytest.mark.parametrize('details,expected',[
    ([{'@type':'type.googleapis.com/google.rpc.ErrorInfo','reason':'API_KEY_INVALID',
       'metadata':{'private':'fabricated-private-key'}}], {'reasons':['API_KEY_INVALID']}),
    ([{'@type':'type.googleapis.com/google.rpc.ErrorInfo','reason':'API_KEY_EXPIRED'}],
     {'reasons':['API_KEY_EXPIRED']}),
    ([{'@type':'type.googleapis.com/google.rpc.BadRequest','fieldViolations':[
        {'field':'generation_config.response_json_schema.properties.topics.items',
         'description':'fabricated-private-key private provider text'},
        {'field':'generationConfig.fabricated-private-key','description':'private'},
        {'field':'generationConfig.arbitrary_private_data'}]}],
     {'field_paths':['generation_config.response_json_schema.properties.topics.items']}),
])
def test_sdk_http400_generic_classification_has_distinct_safe_receipt(inputs,tmp_path,details,expected):
    _,a=inputs;seen=[]
    violation={'http_status':400,'packet':{'error':{'code':400,'status':'INVALID_ARGUMENT',
        'message':'INVALID_ARGUMENT fabricated-private-key private provider text','details':details}}}
    with pytest.raises(Exception): run(tmp_path,a,seen,violation)
    ledger=tmp_path/'ledger/budget.sqlite'; db=sqlite3.connect(ledger)
    status,path,digest,count,cost,error=db.execute(
        'select status,result_path,result_sha,request_count,cost,error_code from reservations').fetchone()
    db.close()
    saved=Path(path);before=saved.read_bytes();receipt=json.loads(before)
    assert (status,count,cost)==('failed',1,COST_MICRO_USD)
    assert error==('GoogleInvalidRequestError:schema' if 'field_paths' in expected
                   else 'GoogleInvalidRequestError:invalid_argument')
    assert receipt['status']=='observed_safe_error_diagnostic' and fingerprint(receipt)==digest
    assert receipt['operation_key']==operation_key(a.spec)
    assert receipt['request_digest']==a.spec.request_digest and receipt['context_digest']==a.spec.context_digest
    assert receipt['diagnostic']['provider_status']=='INVALID_ARGUMENT'
    for key,value in expected.items(): assert receipt['diagnostic'][key]==value
    assert saved.stat().st_mode & 0o777==0o600
    assert b'fabricated-private-key' not in before and b'private provider text' not in before
    assert not receipt['diagnostic']['raw_error_body_saved']
    assert not receipt['diagnostic']['account_tier_verified']
    with pytest.raises(ValueError,match='consumed'):
        execute(a,a.plan_sha256,ledger,tmp_path/'results',ROOT,
            client_factory=lambda **kw:pytest.fail('second client'),key_provider=lambda:pytest.fail('second key'),now=NOW)
    assert saved.read_bytes()==before and len(seen)==1


def test_sdk_http429_free_metric_observation_is_not_account_tier_verification(inputs,tmp_path):
    _,a=inputs;seen=[]
    violation={'http_status':429,'packet':{'error':{'status':'RESOURCE_EXHAUSTED','message':'private',
        'details':[{'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[
            {'quotaMetric':'generativelanguage.googleapis.com/generate_content_free_tier_requests',
             'quotaDimensions':{'model':'gemini-3.1-flash-lite','project':'private'},'description':'private'}]},
            {'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'34s'}]}}}
    with pytest.raises(Exception):run(tmp_path,a,seen,violation)
    db=sqlite3.connect(tmp_path/'ledger/budget.sqlite')
    status,path,count=db.execute('select status,result_path,request_count from reservations').fetchone();db.close()
    diagnostic=json.loads(Path(path).read_text())['diagnostic']
    assert status=='failed' and count==len(seen)==1
    assert diagnostic['provider_status']=='RESOURCE_EXHAUSTED'
    assert diagnostic['quota_metrics']==['generativelanguage.googleapis.com/generate_content_free_tier_requests']
    assert diagnostic['quota_model']=='gemini-3.1-flash-lite' and diagnostic['retry_delay_seconds']==34
    assert not diagnostic['automatic_retry'] and not diagnostic['account_tier_verified']
    assert 'project' not in diagnostic and 'description' not in diagnostic


@pytest.mark.parametrize('packet',[
    None, [], {'error':[]}, {'error':{'status':{},'details':[None,[],{}]}},
    {'error':{'status':'fabricated-private-key','message':'private','details':[
        {'@type':'type.googleapis.com/google.rpc.ErrorInfo','reason':{}},
        {'@type':'type.googleapis.com/google.rpc.ErrorInfo','reason':'fabricated-private-key'},
        {'@type':'type.googleapis.com/google.rpc.BadRequest','fieldViolations':{}},
        {'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[{},None]},
        {'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'9000s'}]}},
])
def test_untrusted_error_details_have_bounded_allowlisted_output(packet):
    from pdf_notion_mvp.provider_diagnostics import safe_error_diagnostic
    response=httpx.Response(400,json=packet)
    diagnostic=safe_error_diagnostic(response,expected_request({'units':[]}), 'fabricated-private-key','gemini-3.1-flash-lite')
    assert diagnostic==dict(http_status=400,provider_status='unclassified',reasons=[],field_paths=[],quota_metrics=[],
        automatic_retry=False,account_tier_verified=False,raw_error_body_saved=False)


@pytest.mark.parametrize('body',[b'not JSON',b'\xff',b'x'*64001])
def test_non_json_and_oversized_error_body_never_saved(body):
    from pdf_notion_mvp.provider_diagnostics import safe_error_diagnostic
    diagnostic=safe_error_diagnostic(httpx.Response(400,content=body),{},'fabricated-private-key','gemini-3.1-flash-lite')
    assert diagnostic['provider_status']=='unclassified' and not diagnostic['raw_error_body_saved']


def test_existing_error_receipt_without_ledger_is_preserved_before_key(inputs,tmp_path):
    _,a=inputs;p=tmp_path/'results'/(operation_key(a.spec)+'-error.json')
    p.parent.mkdir();p.write_text('preserve existing receipt');before=p.read_bytes()
    with pytest.raises(ValueError,match='existing result without receipt'):
        execute(a,a.plan_sha256,tmp_path/'ledger/budget.sqlite',tmp_path/'results',ROOT,
            key_provider=lambda:pytest.fail('key read'),now=NOW)
    assert p.read_bytes()==before


def test_duplicate_id_offline_recovery_preserves_content_original_and_cost(inputs,tmp_path):
    _,a=inputs; seen=[]
    original,_=run(tmp_path,a,seen,'duplicate_ids')
    assert original['errors']==['duplicate_claim_id']
    source=tmp_path/'results'/(operation_key(a.spec)+'.json'); before=source.read_bytes()
    ledger=tmp_path/'ledger/budget.sqlite'
    recovered,status=recover_positional_ids(source,budget_path=ledger)
    assert status=='written' and recovered['status']=='ready_for_content_review' and not recovered['errors']
    assert source.read_bytes()==before and len(seen)==1
    for old,new in zip(original['draft']['topics'],recovered['draft']['topics']):
        assert old['title']==new['title']
        for old_claim,new_claim in zip(old['claims'],new['claims']):
            assert {k:v for k,v in old_claim.items() if k!='claim_id'}=={k:v for k,v in new_claim.items() if k!='claim_id'}
    for old,new in zip(original['draft']['exercises'],recovered['draft']['exercises']):
        assert {k:v for k,v in old.items() if k not in ['question_id','explanation']}=={k:v for k,v in new.items() if k not in ['question_id','explanation']}
        assert old['explanation']['text']==new['explanation']['text'] and old['explanation']['citations']==new['explanation']['citations']
    assert recover_positional_ids(source,budget_path=ledger)==(recovered,'unchanged')
    assert execute(a,a.plan_sha256,ledger,tmp_path/'results',ROOT,now=NOW,
        key_provider=lambda:pytest.fail('recovered result key lookup'),
        client_factory=lambda **kw:pytest.fail('recovered result retry'))==(recovered,'unchanged')
    db=sqlite3.connect(ledger)
    assert db.execute('SELECT status,request_count,cost FROM reservations').fetchone()==('completed',1,COST_MICRO_USD)
    db.close()
    target=source.with_name(original['operation_key']+'-ids.json');target.write_bytes(target.read_bytes()+b' ')
    # Whitespace is harmless JSON; changing actual data must be rejected.
    changed=json.loads(target.read_text());changed['draft']['topics'][0]['title']='changed';target.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='changed'):recover_positional_ids(source,budget_path=ledger)


def test_recovery_rejects_other_failure_without_writing(inputs,tmp_path):
    _,a=inputs;original,_=run(tmp_path,a,[],'citation')
    source=tmp_path/'results'/(operation_key(a.spec)+'.json');ledger=tmp_path/'ledger/budget.sqlite'
    before=source.read_bytes()
    with pytest.raises(ValueError,match='only duplicate'):recover_positional_ids(source,budget_path=ledger)
    assert source.read_bytes()==before and not source.with_name(original['operation_key']+'-ids.json').exists()


def test_message_only_error_keeps_public_terms_without_private_values():
    from pdf_notion_mvp.provider_diagnostics import safe_error_diagnostic
    response=httpx.Response(400,json={'error':{'status':'INVALID_ARGUMENT','message':
        'Invalid schema generation_config.response_json_schema properties topics minItems '
        'fabricated-private-key confidential-note 123456789012'}})
    diagnostic=safe_error_diagnostic(response,expected_request({'units':[]}),'fabricated-private-key','gemini-3.1-flash-lite')
    assert diagnostic['message_terms']==['invalid','schema','generationconfig','responsejsonschema','properties','topics','minitems']
    assert 'fabricated-private-key' not in json.dumps(diagnostic)
    assert 'confidential-note' not in json.dumps(diagnostic) and '123456789012' not in json.dumps(diagnostic)


def test_id_recovery_does_not_trust_claimed_error_list(inputs,tmp_path):
    _,a=inputs;original,_=run(tmp_path,a,[],'duplicate_ids')
    source=tmp_path/'results'/(operation_key(a.spec)+'.json');ledger=tmp_path/'ledger/budget.sqlite'
    original['draft']['topics'][0]['claims'][0]['citations'][0]['quote']='unsupported'
    source.write_text(json.dumps(original))
    with pytest.raises(ValueError,match='otherwise valid'):recover_positional_ids(source,budget_path=ledger)
