import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pdf_notion_mvp.document_audit import audit_document,plan_batches
from pdf_notion_mvp.document_local_cli import run_local,render_audit
from pdf_notion_mvp.local_run import LocalRunStore
from pdf_notion_mvp.local_search import prepare_search,search,terms
from pdf_notion_mvp.rag_cli import load_fixture
from pdf_notion_mvp.rag_files import ReviewFiles
from pdf_notion_mvp.study_bundle import sha,encode

ROOT=Path(__file__).parents[1]


def fixture():
    f=load_fixture(ROOT)
    return ReviewFiles(f.source,f.hierarchy,f.review,f.bindings,None)


def test_full_audit_queue_does_not_promote_candidates_and_reuse_plan_exact_coverage():
    f=fixture();before=[x.model_dump_json() for x in [f.source,f.hierarchy,f.review]]
    a=audit_document(f);plan=plan_batches(a,batch_size=1,reuse_pages=[2])
    assert a['page_count']==len(f.source.document.pages) and a['outline_exact_block_coverage']
    assert plan['all_pages_covered_exactly_once'] and plan['reused_pages']==[2]
    assert all(not b['external_execution_approved'] for b in plan['batches'])
    assert not a['full_document_review_complete'] and not a['ocr_text_semantics_verified']
    assert any('existing_correction_candidate' in c['reasons'] for c in a['correction_queue'])
    assert all(not c['automatic_confirmation'] for c in a['correction_queue'])
    assert before==[x.model_dump_json() for x in [f.source,f.hierarchy,f.review]]


@pytest.mark.parametrize('size,reuse',[(0,[]),(9,[]),(True,[]),(1,[999]),(1,[1,1])])
def test_invalid_batch_plans_block(size,reuse):
    with pytest.raises(ValueError):plan_batches(audit_document(fixture()),batch_size=size,reuse_pages=reuse)


def test_source_and_outline_mismatch_block_before_any_partial_report():
    f=fixture();f.hierarchy.nodes[-1].block_ids.pop()
    with pytest.raises(ValueError):audit_document(f)


def test_explicit_pdf_hash_must_match(tmp_path):
    p=tmp_path/'authored.pdf';p.write_bytes(b'authored fake PDF bytes')
    with pytest.raises(ValueError,match='version mismatch'):audit_document(fixture(),pdf_path=p)


def test_bm25_has_no_answer_or_embedding_and_unreviewed_is_explicit():
    f=fixture();index=prepare_search(f)
    safe=search(f,'후보')
    review=search(f,'후보',include_unreviewed=True)
    assert not any(m['status']=='unreviewed_candidate' for m in safe['matches'])
    assert review['matches'] and any(m['status']=='unreviewed_candidate' for m in review['matches'])
    assert search(f,'비행',include_unreviewed=True)['status']=='unknown'
    assert review['answer'] is None and not review['embedding_calls'] and not index['actual_embeddings_used']
    assert all(m['quote']==m['text'] for m in review['matches'])
    assert search(f,'xyzunmatch999')['status']=='unknown'
    assert search(f,'문맥',saved_index=index)==search(f,'문맥')
    changed=copy.deepcopy(index);changed['records'][0]['text']='changed'
    with pytest.raises(ValueError,match='stale'):search(f,'문맥',saved_index=changed)
    assert terms('ＡＢＣ')==terms('abc')


@pytest.mark.parametrize('q,scope,limit',[('',False,5),('a'*1001,False,5),('x',1,5),('x',False,0),('x',False,True)])
def test_search_input_limits(q,scope,limit):
    with pytest.raises(ValueError):search(fixture(),q,include_unreviewed=scope,limit=limit)


def test_local_checkpoint_restart_preserves_success_and_retries_only_failed_page(tmp_path):
    store=LocalRunStore(tmp_path/'run.sqlite');key='a'*64;seen=[]
    def failing(p):
        seen.append(p)
        if p==2:raise RuntimeError('fabricated-secret-value')
        return {'page':p}
    with pytest.raises(RuntimeError,match='prior successes'):store.process(key,[1,2,3],failing)
    def ok(p):seen.append(p);return {'page':p}
    assert LocalRunStore(tmp_path/'run.sqlite').process(key,[1,2,3],ok)==[{'page':p} for p in [1,2,3]]
    assert seen==[1,2,2,3]
    assert store.process(key,[1,2,3],lambda p:pytest.fail('duplicate work'))==[{'page':p} for p in [1,2,3]]
    assert b'fabricated-secret-value' not in (tmp_path/'run.sqlite').read_bytes()
    db=sqlite3.connect(tmp_path/'run.sqlite');db.execute('UPDATE pages SET payload=? WHERE page=1',(json.dumps({'page':9}),));db.commit();db.close()
    with pytest.raises(ValueError,match='changed'):store.process(key,[1],ok)


def test_checkpoint_concurrency_deduplicates_page_work(tmp_path):
    s=LocalRunStore(tmp_path/'run.sqlite');calls=[]
    def op(p):calls.append(p);return {'page':p}
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:s.process('b'*64,[1,2],op),[1,2]))
    assert results[0]==results[1] and calls==[1,2]


def test_nonowned_database_preserved(tmp_path):
    p=tmp_path/'user.sqlite';db=sqlite3.connect(p);db.execute('CREATE TABLE original(value TEXT)');db.commit();db.close();before=p.read_bytes()
    with pytest.raises(ValueError):LocalRunStore(p)
    assert p.read_bytes()==before


def test_real_file_interface_idempotent_input_sha_and_html_escaping(tmp_path):
    f=fixture();inputs=tmp_path/'input';inputs.mkdir();paths=[]
    for name,v in [('source',f.source),('outline',f.hierarchy),('review',f.review)]:
        p=inputs/(name+'.json');p.write_text(v.model_dump_json());paths.append(p)
    before=[p.read_bytes() for p in paths]
    final,status=run_local(paths,output_dir=tmp_path/'output',query='<script>x</script>',include_unreviewed=True)
    assert status=='written' and '&lt;script&gt;' in (final/'index.html').read_text()
    times={p.name:p.stat().st_mtime_ns for p in final.iterdir()}
    assert run_local(paths,output_dir=tmp_path/'output',query='<script>x</script>',include_unreviewed=True)==(final,'unchanged')
    assert times=={p.name:p.stat().st_mtime_ns for p in final.iterdir()} and before==[p.read_bytes() for p in paths]
    (final/'audit.json').write_text('{}')
    with pytest.raises(ValueError):run_local(paths,output_dir=tmp_path/'output',query='<script>x</script>',include_unreviewed=True)


def test_modified_audit_cannot_reuse_old_batch_identity():
    a=audit_document(fixture());a['pages'][0]['text_blocks']+=1
    with pytest.raises(ValueError,match='digest'):plan_batches(a)
