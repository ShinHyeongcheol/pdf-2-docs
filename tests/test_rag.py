import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from pdf_notion_mvp.contracts import ExtractionInfo, FixtureInput, ImageBlock
from pdf_notion_mvp.rag import (
    UNKNOWN, LocalLexicalAdapter, MockAnswerAdapter, RagWorkflow, extractive_draft, prepare_index,
)
from pdf_notion_mvp.rag_cli import EvalSet, evaluate, load_fixture, main, revised_fixture, run_fixture
from pdf_notion_mvp.review import document_digest

ROOT=Path(__file__).parents[1]


@pytest.fixture
def rag(): return load_fixture(ROOT)


def index_for(f): return prepare_index(f.source,f.hierarchy,f.review,f.bindings)


def test_confirmed_index_excludes_candidates_and_preserves_original_provenance(rag):
    before=rag.model_dump_json()
    index=index_for(rag)
    assert [e.source_block_id for e in index.entries]==['rag-checkpoint','rag-corrected','rag-budget','rag-version']
    corrected=index.entries[1]
    assert corrected.text==rag.review.corrections[0].proposed_text
    assert corrected.original_text==rag.review.corrections[0].original_text
    assert corrected.layer=='confirmed_transcription' and corrected.correction_id=='rag-fix'
    assert corrected.source.page==1 and corrected.section_id=='rag.control'
    assert corrected.notion_link=='https://www.notion.so/00000000000040008000000000000004'
    assert index.entries[-1].source.page==2 and index.entries[-1].notion_link is None
    assert not index.actual_embeddings_used and not index.vector_search_implemented
    assert rag.model_dump_json()==before
    result=run_fixture(rag,'인용 검증')
    citation=result.draft.citations[0]
    assert citation.quote==corrected.text and citation.source==corrected.source
    assert citation.notion_link==corrected.notion_link
    assert result.events==['retrieve','generate','independent_verify']
    assert result.status=='ready_for_review' and result.human_review_required
    assert not result.semantic_correctness_verified and not result.notion_links_verified


@pytest.mark.parametrize('question',['미확정 비행','파생 초능력','합니디','synthetic_secret_code','미색인 값','해왕성 질량'])
def test_no_evidence_unknown_skips_generator_including_candidates(rag,question):
    generator=MockAnswerAdapter(RuntimeError('must not be invoked'))
    result=run_fixture(rag,question,generator)
    assert result.status=='unknown' and result.draft.answer==UNKNOWN
    assert not result.draft.citations and not result.retrieved_ids
    assert generator.calls==0 and result.generation_skipped


def test_langchain_lexical_retriever_is_deterministic_and_locally_ranked(rag):
    retriever=LocalLexicalAdapter().build(index_for(rag))
    assert isinstance(retriever,BaseRetriever)
    one=retriever.invoke('문서 수정')
    two=retriever.invoke('문서 수정')
    assert [d.metadata['evidence']['source_block_id'] for d in one]==['rag-version','rag-corrected']
    assert one==two
    one[0].metadata['evidence']['text']='modified copy'
    assert retriever.invoke('문서 수정')==two


@pytest.mark.parametrize('field',['quote','answer','document','version','block','section','page','bbox','digest','index_digest','layer','correction','link','duplicate','no_citations','unknown_with_evidence','extra_claim'])
def test_wrong_citation_or_unsupported_answer_requires_review(rag,field):
    class Attack(MockAnswerAdapter):
        def respond(self,context):
            self.calls+=1
            draft=extractive_draft(context).model_dump(mode='json')
            c=draft['citations'][0]
            if field=='quote': c['quote']='발명한 수치 999'
            if field=='answer': draft['answer']='발명한 결론'
            if field=='document': c['source']['document_id']='other-synthetic-document'
            if field=='version': c['source']['version']='other'
            if field=='block': c['source_block_id']='rag-budget'
            if field=='section': c['section_id']='rag.version'
            if field=='page': c['source']['page']=2
            if field=='bbox': c['source']['bbox']['x0']+=1
            if field=='digest': c['source_digest']='0'*64
            if field=='index_digest': c['index_digest']='0'*64
            if field=='layer': c['layer']='confirmed_transcription'
            if field=='correction': c['correction_id']='invented'
            if field=='link': c['notion_link']='https://www.notion.so/other'
            if field=='duplicate': draft['citations']*=2
            if field=='no_citations': draft['citations']=[]
            if field=='unknown_with_evidence': draft.update(kind='unknown',answer=UNKNOWN,citations=[])
            if field=='extra_claim': draft['freeform_summary']='unsupported'
            if field=='quote': draft['answer']='근거 원문:\n'+c['quote']
            return draft
    generator=Attack()
    result=run_fixture(rag,'재개 체크포인트',generator)
    assert result.status=='failed_human_review' and result.draft is None
    assert result.errors and generator.calls==1


@pytest.mark.parametrize('change',['candidate','foreign_document','wrong_content','wrong_section','duplicate','irrelevant','too_many'])
def test_retrieval_adapter_cannot_inject_unindexed_or_mixed_evidence(rag,change):
    current=index_for(rag)
    entry=current.entries[0].model_dump(mode='json')
    if change=='candidate': entry.update(source_block_id='rag-candidate',text='미확정 비행 후보',evidence_id='a'*64)
    if change=='foreign_document': entry['source']['document_id']='foreign-synthetic'
    if change=='wrong_section': entry['section_id']='rag.version'
    if change=='irrelevant': entry=current.entries[2].model_dump(mode='json')
    docs=[Document(page_content=entry['text'],metadata={'evidence':entry})]
    if change=='wrong_content': docs[0].page_content='invented 999'
    if change=='duplicate': docs*=2
    if change=='too_many': docs*=4
    class Injected(BaseRetriever):
        documents: list[Document]
        def _get_relevant_documents(self,query,*,run_manager): return self.documents
    class Adapter:
        mode='lexical_local'
        def build(self,index): return Injected(documents=docs)
    generator=MockAnswerAdapter()
    result=RagWorkflow(generator,Adapter()).run(rag.source,rag.hierarchy,rag.review,'재개 체크포인트',rag.bindings)
    assert result.status=='failed_human_review' and generator.calls==0
    assert result.events==['retrieve','generate','independent_verify']


def test_generator_mutation_does_not_change_independent_context(rag):
    before=rag.model_dump_json()
    class Mutating(MockAnswerAdapter):
        def respond(self,context):
            context.retrieved[0].text='재개 기능은 발명한 복구점 999부터 시작합니다.'
            context.retrieved[0].source.page=2
            return extractive_draft(context)
    result=run_fixture(rag,'재개 체크포인트',Mutating())
    assert result.status=='failed_human_review' and rag.model_dump_json()==before


def test_retrieval_builder_mutation_does_not_change_authority(rag):
    class Mutating(LocalLexicalAdapter):
        def build(self,index):
            index.entries[0].text='재개 체크포인트 발명 999'
            return super().build(index)
    result=RagWorkflow(retrieval=Mutating()).run(rag.source,rag.hierarchy,rag.review,'재개 체크포인트',rag.bindings)
    assert result.status=='failed_human_review'


@pytest.mark.parametrize('target',['generator','embedding'])
def test_actual_generation_and_embedding_modes_block_without_calls(rag,target):
    class LiveGenerator:
        mode='gemini'
        def generate(self,context): pytest.fail('actual generation must not be called')
    class Vector:
        mode='embedding_vector'
        def build(self,index): pytest.fail('actual embeddings must not be called')
    workflow=RagWorkflow(LiveGenerator() if target=='generator' else None,Vector() if target=='embedding' else None)
    result=workflow.run(rag.source,rag.hierarchy,rag.review,'재개 체크포인트',rag.bindings)
    assert result.status=='failed_human_review'
    assert not result.actual_embeddings_used and not result.vector_search_implemented


def test_inherited_tracing_disabled_for_retrieve_and_generate(rag,monkeypatch):
    from langsmith import tracing_context
    from langsmith.run_helpers import get_tracing_context
    monkeypatch.setenv('LANGSMITH_TRACING','true')
    monkeypatch.setenv('LANGSMITH_API_KEY','fabricated-trace-key')
    class CheckedRetrieval(LocalLexicalAdapter):
        def build(self,index):
            assert get_tracing_context()['enabled'] is False
            return super().build(index)
    class CheckedGenerator(MockAnswerAdapter):
        def respond(self,context):
            assert get_tracing_context()['enabled'] is False
            return super().respond(context)
    with tracing_context(enabled=True):
        result=RagWorkflow(CheckedGenerator(),CheckedRetrieval()).run(rag.source,rag.hierarchy,rag.review,'재개 체크포인트',rag.bindings)
    assert result.status=='ready_for_review'


@pytest.mark.parametrize('change',['source','review','hierarchy','link','mode','flag','index_tamper','foreign_source'])
def test_saved_index_staleness_rejected_before_generator(rag,change):
    index=index_for(rag)
    if change=='source':
        rag.source.document.blocks[1].text+=' 수정 문구'
        rag.review.source_digest=document_digest(rag.source.document)
    if change=='review': rag.review.corrections[0].proposed_text+=' 수정 교정'
    if change=='hierarchy': rag.hierarchy.nodes[1].title='수정 목차'
    if change=='link': rag.bindings[0].page_id=UUID('00000000-0000-4000-8000-000000000099')
    if change=='mode': index.mode='embedding_vector'
    if change=='flag': index.actual_embeddings_used=True
    if change=='index_tamper': index.entries[0].text+=' 악의적 추가'
    if change=='foreign_source': index.document_id='foreign-synthetic-document'
    generator=MockAnswerAdapter()
    with pytest.raises(ValueError): run_fixture(rag,'재개 체크포인트',generator,index=index)
    assert generator.calls==0


def test_modified_document_reindex_changes_ids_and_returns_only_new_text(rag):
    old=index_for(rag)
    updated=revised_fixture(rag)
    with pytest.raises(ValueError): run_fixture(updated,'복구지점부터',index=old)
    new=index_for(updated)
    assert new.index_digest!=old.index_digest
    assert not {e.evidence_id for e in old.entries}&{e.evidence_id for e in new.entries}
    result=run_fixture(updated,'복구지점부터',index=new)
    assert result.status=='ready_for_review' and result.version=='authored-v2'
    assert result.draft.citations[0].source.version=='authored-v2'
    assert '복구지점' in result.draft.answer and '체크포인트' not in result.draft.answer
    assert run_fixture(updated,'체크포인트').status=='unknown'


@pytest.mark.parametrize('change',['unknown_section','wrong_document','wrong_version','duplicate'])
def test_notion_bindings_validated_locally_without_notion_access(rag,change):
    if change=='unknown_section': rag.bindings[0].section_id='missing'
    if change=='wrong_document': rag.bindings[0].document_id='other'
    if change=='wrong_version': rag.bindings[0].version='other'
    if change=='duplicate': rag.bindings*=2
    with pytest.raises(ValueError): index_for(rag)


def test_ocr_shaped_mock_ir_indexes_only_confirmed_transcription(rag,tmp_path):
    # Authored mock raster bytes/IR only; no actual OCR or image decoding.
    document=rag.source.document.model_copy(deep=True)
    document.extraction=ExtractionInfo(engine='authored-mock-ocr',pages_processed=[1,2],human_review_required=True)
    for block in document.blocks:
        block.source.method='ocr'
        block.source.confidence=0.7
    for page in [1,2]:
        path=tmp_path/f'authored-raster-{page}.dat'
        payload=f'authored mock raster {page}'.encode()
        path.write_bytes(payload)
        if page==1: image=next(b for b in document.blocks if b.kind=='image')
        else:
            template=document.blocks[4].model_dump()
            template['block_id']='rag-raster-2'
            template['source']['page']=2
            image=ImageBlock.model_validate(template)
            document.blocks.append(image)
            rag.hierarchy.nodes[-1].block_ids.append(image.block_id)
        image.asset_ref=str(path)
        image.sha256=hashlib.sha256(payload).hexdigest()
        image.source.method='raster'
        image.source.confidence=None
        image.source.bbox.x0=image.source.bbox.y0=0
        image.source.bbox.x1=600
        image.source.bbox.y1=800
    source=FixtureInput(kind='ocr_ir',document=document)
    for correction in rag.review.corrections:
        correction.source=next(b.source.model_copy(deep=True) for b in document.blocks if b.block_id==correction.block_id)
    rag.review.source_digest=document_digest(document)
    index=prepare_index(source,rag.hierarchy,rag.review,rag.bindings,asset_root=tmp_path)
    assert [e.source_block_id for e in index.entries]==['rag-corrected']
    assert index.entries[0].layer=='confirmed_transcription'
    result=RagWorkflow().run(source,rag.hierarchy,rag.review,'인용 검증',rag.bindings,asset_root=tmp_path)
    assert result.status=='ready_for_review' and result.draft.citations[0].source.method=='ocr'
    assert RagWorkflow().run(source,rag.hierarchy,rag.review,'재개 체크포인트',rag.bindings,asset_root=tmp_path).status=='unknown'


def test_small_authored_eval_set_covers_all_requested_cases(rag):
    cases=EvalSet.model_validate_json((ROOT/'fixtures/synthetic-rag-eval.json').read_text()).cases
    assert {c.kind for c in cases}=={'answerable','no_evidence','wrong_quote','candidate','reindex','source_mix'}
    outcome=evaluate(rag,cases)
    assert outcome['passed'] and len(outcome['cases'])==9
    assert next(r for r in outcome['cases'] if r['kind']=='reindex')['stale_index_rejected'] is True


@pytest.fixture
def cli_root(tmp_path):
    (tmp_path/'fixtures').mkdir()
    for name in ['synthetic-rag.json','synthetic-rag-eval.json']:
        (tmp_path/'fixtures'/name).write_bytes((ROOT/'fixtures'/name).read_bytes())
    return tmp_path


def run_cli(root,args):
    with pytest.raises(SystemExit) as exc: main(args,project_root=root)
    return exc.value.code


def test_cli_answer_unknown_and_eval_without_credentials(cli_root,monkeypatch):
    old_read=Path.read_text
    def guarded(path,*args,**kwargs):
        assert not path.name.startswith('.env')
        return old_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',guarded)
    monkeypatch.setattr('pdf_notion_mvp.providers.build_provider',lambda *a,**k: pytest.fail('no providers'))
    assert run_cli(cli_root,[])==0
    data=json.loads((cli_root/'output/mock-rag.json').read_text())
    assert data['status']=='ready_for_review' and data['mock_calls']==1
    assert data['actual_key_reads']==data['actual_model_requests']==data['notion_requests']==0
    assert not data['actual_embeddings_used'] and not data['vector_search_implemented']
    assert run_cli(cli_root,['--question','해왕성 질량'])==0
    data=json.loads((cli_root/'output/mock-rag.json').read_text())
    assert data['status']=='unknown' and data['mock_calls']==0
    assert run_cli(cli_root,['--eval'])==0
    assert json.loads((cli_root/'output/mock-rag.json').read_text())['evaluation']['passed']


@pytest.mark.parametrize('args',[
    ['--output','fixtures/synthetic-rag.json'],['--output','output/live-runs/x.json'],
    ['--output','../escape.json'],['--output','output/x.txt'],['--question',''],['--question','x'*1001],
])
def test_cli_rejects_unsafe_output_and_invalid_question(cli_root,args):
    original=(cli_root/'fixtures/synthetic-rag.json').read_bytes()
    assert run_cli(cli_root,args)==1
    assert (cli_root/'fixtures/synthetic-rag.json').read_bytes()==original


def test_mock_errors_do_not_expose_payload_or_retry(rag):
    generator=MockAnswerAdapter(TimeoutError('fabricated-secret-do-not-expose'))
    result=run_fixture(rag,'재개 체크포인트',generator)
    assert result.status=='failed_human_review' and generator.calls==1
    assert result.errors==['generate_error:TimeoutError']
    assert 'fabricated-secret' not in result.model_dump_json()


@pytest.mark.parametrize("kind",["symlink","hardlink"])
def test_cli_rejects_linked_output(cli_root,kind):
    import os
    (cli_root/"output").mkdir()
    source=cli_root/"fixtures/synthetic-rag.json"
    output=cli_root/"output/linked.json"
    before=source.read_bytes()
    if kind=="symlink": output.symlink_to(source)
    else: os.link(source,output)
    assert run_cli(cli_root,["--output","output/linked.json"])==1
    assert source.read_bytes()==before


@pytest.mark.parametrize("failure",["partial_write","replace"])
def test_cli_atomic_output_failure_preserves_previous_result(cli_root,monkeypatch,capsys,failure):
    from pdf_notion_mvp import rag_cli
    assert run_cli(cli_root,[])==0
    output=cli_root/"output/mock-rag.json"
    original=output.read_bytes()
    old_dump=json.dump
    old_replace=Path.replace
    def partial(value,stream,*args,**kwargs):
        stream.write('{"incomplete":')
        stream.flush()
        raise OSError("fabricated-secret-do-not-expose")
    def fail_replace(path,target): raise OSError("fabricated-secret-do-not-expose")
    with monkeypatch.context() as scoped:
        if failure=="partial_write": scoped.setattr(rag_cli.json,"dump",partial)
        else: scoped.setattr(Path,"replace",fail_replace)
        assert run_cli(cli_root,["--question","해왕성 질량"])==1
    assert output.read_bytes()==original
    assert not list(output.parent.glob(".mock-rag.json-*"))
    assert run_cli(cli_root,["--question","해왕성 질량"])==0
    assert json.loads(output.read_text())["status"]=="unknown"
    assert "fabricated-secret" not in capsys.readouterr().out


@pytest.mark.parametrize('kind,status',[('code','confirmed'),('code','candidate')])
def test_grouped_code_lineage_cannot_become_answer_evidence(rag,kind,status):
    from pdf_notion_mvp.review import DerivedFragment
    before=index_for(rag)
    rag.review.fragments.append(DerivedFragment(fragment_id='body-group',section_id='rag.control',
        source_block_ids=['rag-corrected'],kind=kind,status=status,text='독립 합성 그룹',
        basis='본문 역할 OCR 블록의 그룹 분류 회귀',correction_ids=['rag-fix']))
    current=index_for(rag)
    assert 'rag-corrected' not in {entry.source_block_id for entry in current.entries}
    generator=MockAnswerAdapter(RuntimeError('excluded group must not invoke generator'))
    result=run_fixture(rag,'인용 검증',generator)
    assert result.status=='unknown' and result.generation_skipped and generator.calls==0
    with pytest.raises(ValueError,match='stale'):run_fixture(rag,'인용 검증',index=before)
