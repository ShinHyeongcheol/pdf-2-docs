import hashlib
import json
from pathlib import Path

import pytest

from pdf_notion_mvp.contracts import FixtureInput
from pdf_notion_mvp.rag import IndexLimitExceeded, MockAnswerAdapter, RagWorkflow, prepare_index
from pdf_notion_mvp.rag_cli import load_fixture, main
from pdf_notion_mvp.rag_files import MAX_INPUT_BYTES, load_review_files
from pdf_notion_mvp.review import HierarchicalOutline, ReviewLayer, document_digest

ROOT=Path(__file__).parents[1]


def run_cli(root,args):
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    return exc.value.code


def save_inputs(root,source,hierarchy,review,bindings=None):
    root.mkdir(parents=True,exist_ok=True)
    for name,model in [('source.json',source),('outline.json',hierarchy),('review.json',review)]:
        (root/name).write_text(model.model_dump_json())
    if bindings:
        (root/'binding.json').write_text(bindings[0].model_dump_json())
    return ['--source',str(root/'source.json'),'--outline',str(root/'outline.json'),'--review',str(root/'review.json')]


@pytest.fixture
def files(tmp_path):
    fixture=load_fixture(ROOT)
    args=save_inputs(tmp_path/'input',fixture.source,fixture.hierarchy,fixture.review,fixture.bindings)
    return tmp_path,fixture,args


def test_file_cli_subset_preserves_full_input_and_binding(files):
    root,f,args=files
    protected=[p for p in (root/'input').iterdir()]
    before={p:p.read_bytes() for p in protected}
    args+=['--section','rag.control','--question','인용 검증','--binding',str(root/'input/binding.json')]
    assert run_cli(root,args)==0
    data=json.loads((root/'output/mock-rag.json').read_text())
    assert data['input_mode']=='review_files' and data['source_ir_unmodified']
    assert data['source_pages']==2 and data['source_blocks']==10
    assert data['result']['indexed_entries']==2 and data['result']['selected_section_ids']==['rag.control']
    citation=data['result']['draft']['citations'][0]
    assert citation['source_block_id']=='rag-corrected' and citation['correction_id']=='rag-fix'
    assert citation['notion_link']=='https://www.notion.so/00000000000040008000000000000004'
    assert all(p.read_bytes()==before[p] for p in protected)
    assert data['actual_key_reads']==data['actual_model_requests']==data['notion_requests']==0


@pytest.mark.parametrize('question',['미확정 비행','파생 초능력','비용 검토','후보 대상의'])
def test_candidate_and_outside_selected_leaf_never_supply_answers(files,question):
    root,_,args=files
    assert run_cli(root,args+['--section','rag.control','--question',question])==0
    data=json.loads((root/'output/mock-rag.json').read_text())
    assert data['status']=='unknown' and data['mock_calls']==0 and not data['result']['draft']['citations']


def make_large(pages=162):
    doc_id='authored-large-rag'
    def provenance(page,y): return {'document_id':doc_id,'version':'authored-large-v1','page':page,'bbox':{'x0':40,'y0':y,'x1':550,'y1':y+20}}
    blocks=[]
    nodes=[{'node_id':'large','title':'작성한 대용량 장','source_pages':list(range(1,pages+1))}]
    for number in range(1,pages+1):
        ids=[]
        heading=f'heading-{number}'
        blocks.append({'block_id':heading,'kind':'text','role':'heading','text':f'합성 절 {number}','source':provenance(number,20)})
        ids.append(heading)
        for i in range(4):
            block_id=f'body-{number}-{i}'
            blocks.append({'block_id':block_id,'kind':'text','text':'합성 복구지점 설명. '+('직접 작성한 긴 문장으로 대용량 파일 연결을 검증합니다. '*40),'source':provenance(number,60+i*30)})
            ids.append(block_id)
        nodes.append({'node_id':f'leaf-{number}','parent_id':'large','title':f'합성 절 {number}','source_pages':[number],'block_ids':ids})
    source=FixtureInput.model_validate({'document':{'document_id':doc_id,'version':'authored-large-v1','name':'작성한 대용량 문서','pages':[{'number':n,'width':600,'height':800} for n in range(1,pages+1)],'blocks':blocks}})
    hierarchy=HierarchicalOutline.model_validate({'document_id':doc_id,'version':'authored-large-v1','nodes':nodes})
    review=ReviewLayer(document_id=doc_id,version=source.document.version,source_digest=document_digest(source.document))
    return source,hierarchy,review


def test_authored_large_162_page_file_with_selected_scope_keeps_all_ir(tmp_path):
    source,hierarchy,review=make_large()
    args=save_inputs(tmp_path/'input',source,hierarchy,review)
    path=tmp_path/'input/source.json'
    assert 100_000<path.stat().st_size<MAX_INPUT_BYTES
    before=path.read_bytes()
    assert run_cli(tmp_path,args+['--section','leaf-1','--section','leaf-162','--question','복구지점'])==0
    data=json.loads((tmp_path/'output/mock-rag.json').read_text())
    assert data['status']=='ready_for_review' and data['source_pages']==162 and data['source_blocks']==810
    assert data['result']['indexed_entries']==8 and data['source_ir_unmodified']
    assert path.read_bytes()==before
    assert {c['section_id'] for c in data['result']['draft']['citations']}<= {'leaf-1','leaf-162'}
    assert data['result']['source_digest']==document_digest(source.document)


def test_index_200_bound_is_exact_and_never_truncates_outside_scope(tmp_path,capsys):
    source,hierarchy,review=make_large(51)
    before=source.model_dump_json()
    selected=[f'leaf-{i}' for i in range(1,51)]
    exact=prepare_index(source,hierarchy,review,section_ids=selected)
    assert len(exact.entries)==200
    with pytest.raises(IndexLimitExceeded): prepare_index(source,hierarchy,review)
    with pytest.raises(IndexLimitExceeded): prepare_index(source,hierarchy,review,section_ids=selected+['leaf-51'])
    assert source.model_dump_json()==before
    args=save_inputs(tmp_path/'input',source,hierarchy,review)
    many=sum([['--section',s] for s in selected+['leaf-51']],[])
    assert run_cli(tmp_path,args+many+['--question','복구지점'])==1
    assert not (tmp_path/'output/mock-rag.json').exists()
    assert 'IndexLimitExceeded' in capsys.readouterr().out


@pytest.mark.parametrize('sections',[[],['missing'],['rag-unit'],['rag.control','rag.control']])
def test_invalid_section_scope_fails_before_generation(files,sections):
    _,f,_=files
    generator=MockAnswerAdapter()
    with pytest.raises(ValueError): RagWorkflow(generator).run(f.source,f.hierarchy,f.review,'인용 검증',f.bindings,section_ids=sections)
    assert generator.calls==0


def test_section_order_canonical_and_changed_scope_invalidates_old_index(files):
    _,f,_=files
    one=prepare_index(f.source,f.hierarchy,f.review,f.bindings,section_ids=['rag.version','rag.control'])
    two=prepare_index(f.source,f.hierarchy,f.review,f.bindings,section_ids=['rag.control','rag.version'])
    assert one==two and one.selected_section_ids==['rag.control','rag.version']
    with pytest.raises(ValueError): RagWorkflow().run(f.source,f.hierarchy,f.review,'인용 검증',f.bindings,index=one,section_ids=['rag.control'])


@pytest.mark.parametrize('damage',['missing_source','missing_outline','missing_review','json','schema','digest','version','unselected_source','binding','duplicate_binding'])
def test_missing_damaged_or_mixed_files_fail_without_output(files,damage):
    root,_,args=files
    path=root/'input/source.json'
    if damage.startswith('missing_'): (root/'input'/({'missing_source':'source.json','missing_outline':'outline.json','missing_review':'review.json'}[damage])).unlink()
    if damage=='json': path.write_text('{broken')
    if damage=='schema': path.write_text('{}')
    if damage=='digest':
        p=root/'input/review.json'; d=json.loads(p.read_text()); d['source_digest']='0'*64;p.write_text(json.dumps(d))
    if damage=='version':
        p=root/'input/outline.json';d=json.loads(p.read_text());d['version']='other';p.write_text(json.dumps(d))
    if damage=='unselected_source':
        d=json.loads(path.read_text());d['document']['blocks'][-1]['source']['bbox']['x1']=999;path.write_text(json.dumps(d))
    if damage=='binding':
        p=root/'input/binding.json';d=json.loads(p.read_text());d['document_id']='foreign';p.write_text(json.dumps(d));args+=['--binding',str(p)]
    if damage=='duplicate_binding': args+=['--binding',str(root/'input/binding.json')]*2
    assert run_cli(root,args+['--section','rag.control','--question','인용 검증'])==1
    assert not (root/'output/mock-rag.json').exists()


@pytest.mark.parametrize('case',['partial','no_section','no_question','eval','binding_only','section_only'])
def test_file_mode_arguments_must_be_complete(files,case):
    root,_,args=files
    if case=='partial': args=args[:2]+['--section','rag.control','--question','인용 검증']
    if case=='no_section': args+=['--question','인용 검증']
    if case=='no_question': args+=['--section','rag.control']
    if case=='eval': args+=['--section','rag.control','--question','인용 검증','--eval']
    if case=='binding_only': args=['--binding',str(root/'input/binding.json')]
    if case=='section_only': args=['--section','rag.control']
    assert run_cli(root,args)==1


@pytest.mark.parametrize('target',['source','outline','review','binding'])
def test_output_never_overwrites_any_input_even_under_output_folder(files,target):
    root,f,_=files
    args=save_inputs(root/'output',f.source,f.hierarchy,f.review,f.bindings)
    path=root/'output'/f'{target}.json'
    before=path.read_bytes()
    args+=['--binding',str(root/'output/binding.json'),'--section','rag.control','--question','인용 검증','--output',str(path)]
    assert run_cli(root,args)==1
    assert path.read_bytes()==before


@pytest.mark.parametrize('kind',['symlink','hardlink','dotenv_name','too_large'])
def test_explicit_input_reader_blocks_links_config_and_size(files,kind,monkeypatch):
    import os
    root,_,args=files
    source=root/'input/source.json'
    if kind in {'symlink','hardlink'}:
        alias=root/'input/alias.json'
        if kind=='symlink': alias.symlink_to(source)
        else: os.link(source,alias)
        args[1]=str(alias)
    if kind=='dotenv_name':
        path=root/'input/.env.fixture.json';path.write_text(source.read_text());args[1]=str(path)
        original=Path.read_text
        def checked(p,*a,**k):
            assert p!=path
            return original(p,*a,**k)
        monkeypatch.setattr(Path,'read_text',checked)
    if kind=='too_large': source.write_bytes(b'x'*(MAX_INPUT_BYTES+1))
    assert run_cli(root,args+['--section','rag.control','--question','인용 검증'])==1


def make_ocr_files(root,raster_name='raster.dat'):
    root.mkdir(parents=True,exist_ok=True)
    raster=root/raster_name;payload=b'authored mock raster, not decoded as image';raster.write_bytes(payload)
    def src(y): return {'document_id':'authored-ocr-files','version':'ocr-mock-v1','page':1,'bbox':{'x0':40,'y0':y,'x1':550,'y1':y+20},'method':'ocr','confidence':0.7}
    confirmed={'block_id':'ocr-confirmed','kind':'text','text':'원본 오탈자','source':src(60)}
    candidate={'block_id':'ocr-candidate','kind':'text','text':'미확정 원문','source':src(100)}
    raw={'block_id':'ocr-raw','kind':'text','text':'무교정 원문','source':src(140)}
    source=FixtureInput.model_validate({'kind':'ocr_ir','document':{'document_id':'authored-ocr-files','version':'ocr-mock-v1','name':'작성한 OCR 모양 IR','pages':[{'number':1,'width':600,'height':800}],'blocks':[confirmed,candidate,raw,{'block_id':'raster','kind':'image','asset_ref':str(raster),'sha256':hashlib.sha256(payload).hexdigest(),'source':{'document_id':'authored-ocr-files','version':'ocr-mock-v1','page':1,'bbox':{'x0':0,'y0':0,'x1':600,'y1':800},'method':'raster'}}], 'extraction':{'engine':'authored-mock-ocr','pages_processed':[1],'human_review_required':True}}})
    hierarchy=HierarchicalOutline.model_validate({'document_id':source.document.document_id,'version':source.document.version,'nodes':[{'node_id':'ocr-leaf','title':'합성 OCR 절','source_pages':[1],'block_ids':['ocr-confirmed','ocr-candidate','ocr-raw','raster']}]})
    corrections=[]
    for b,status,text in [(confirmed,'confirmed','확정 인용은 출처를 보존합니다.'),(candidate,'candidate','비행 후보 유입 금지')]:
        corrections.append({'correction_id':'fix-'+b['block_id'],'block_id':b['block_id'],'original_text':b['text'],'source':b['source'],'proposed_text':text,'status':status,'basis':'합성 교정 근거','reviewer':'authored-reviewer','evidence_block_id':'raster','evidence_bbox':b['source']['bbox']})
    review=ReviewLayer.model_validate({'document_id':source.document.document_id,'version':source.document.version,'source_digest':document_digest(source.document),'corrections':corrections})
    return save_inputs(root,source,hierarchy,review),raster


def test_file_cli_ocr_shape_confirms_only_selected_transcription(tmp_path):
    args,raster=make_ocr_files(tmp_path/'input')
    before=raster.read_bytes()
    assert run_cli(tmp_path,args+['--section','ocr-leaf','--question','확정 인용'])==0
    data=json.loads((tmp_path/'output/mock-rag.json').read_text())
    assert data['result']['indexed_entries']==1 and data['result']['draft']['citations'][0]['source_block_id']=='ocr-confirmed'
    assert run_cli(tmp_path,args+['--section','ocr-leaf','--question','무교정 원문'])==0
    assert json.loads((tmp_path/'output/mock-rag.json').read_text())['status']=='unknown'
    assert raster.read_bytes()==before


@pytest.mark.parametrize('damage',['missing','changed','outside','overwrite'])
def test_ocr_trusted_assets_protected_before_generation(tmp_path,damage):
    args,raster=make_ocr_files(tmp_path/'output','raster.json')
    before=raster.read_bytes()
    if damage=='missing': raster.unlink()
    if damage=='changed': raster.write_bytes(b'changed mock raster')
    if damage=='outside':
        outside=tmp_path/'outside.dat';outside.write_bytes(before)
        p=tmp_path/'output/source.json';d=json.loads(p.read_text());d['document']['blocks'][-1]['asset_ref']=str(outside);p.write_text(json.dumps(d))
    output=tmp_path/'output/result.json'
    if damage=='overwrite': output=raster
    assert run_cli(tmp_path,args+['--section','ocr-leaf','--question','확정 인용','--output',str(output)])==1
    if damage=='overwrite': assert raster.read_bytes()==before
    else: assert not output.exists()


def test_file_mode_never_reads_dotenv_or_builds_provider(files,monkeypatch):
    root,_,args=files
    from pdf_notion_mvp import providers
    def blocked(*a,**k): pytest.fail('key/provider access forbidden in file mode')
    monkeypatch.setattr(providers,'selected_key',blocked)
    monkeypatch.setattr(providers,'build_provider',blocked)
    original=Path.read_text
    def guarded(p,*a,**k):
        assert not p.name.startswith('.env')
        return original(p,*a,**k)
    monkeypatch.setattr(Path,'read_text',guarded)
    assert run_cli(root,args+['--section','rag.control','--question','인용 검증'])==0
