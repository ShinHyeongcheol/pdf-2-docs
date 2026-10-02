"""Offline full-document audit/search/batch plan and resumable page checkpoints."""
import argparse
import html
import json
import os
import shutil
import tempfile
from pathlib import Path

from .document_audit import audit_document,plan_batches
from .local_run import LocalRunStore
from .local_search import search
from .rag_files import load_review_files
from .study_bundle import check_existing,encode,private_destination,sha


def render_audit(audit,query):
    esc=lambda v:html.escape(str(v),quote=True)
    out=['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src file:; base-uri \'none\'; form-action \'none\'">',
        '<title>전체 문서 로컬 검수</title><style>body{max-width:1000px;margin:2rem auto;padding:0 1rem;font:17px/1.7 sans-serif;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}td,th{border:1px solid #ddd;padding:8px;text-align:left}pre{white-space:pre-wrap}article{padding:16px 0;border-top:1px solid #ddd}summary{cursor:pointer}</style>',
        '<h1>전체 문서 로컬 검수</h1><p>출처 번호는 실제 PDF 페이지 번호입니다. 슬라이드 하단 인쇄 번호와 다를 수 있습니다.</p>',
        f'<p>{audit["page_count"]}쪽 · {audit["block_count"]}블록 · {audit["leaf_count"]}개 leaf 절 · 확정 교정 {audit["confirmed_text_blocks"]}블록</p>',
        '<p>원본 보존·목차 커버리지 검사와 OCR 의미 정확성은 별도입니다. 미검수 전사·표·코드·그림은 후보이며 자동 확정하지 않습니다.</p>',
        '<nav><a href="#coverage">페이지 커버리지</a> · <a href="#queue">교정 후보</a> · <a href="#search">로컬 검색</a></nav>',
        '<h2 id="coverage">페이지 커버리지</h2><table><tr><th>PDF 쪽</th><th>절</th><th>텍스트/원본 이미지</th><th>검토 상태</th></tr>']
    for p in audit['pages']:
        out.append(f'<tr><td>{p["page"]}</td><td>{esc(", ".join(p["section_ids"]))}</td><td>{p["text_blocks"]} / {p["raster_blocks"]}</td><td>{esc(p["review_counts"])}</td></tr>')
    out.append('</table><h2 id="queue">교정 후보 · 자동 확정 아님</h2>')
    for c in audit['correction_queue']:
        out.append(f'<details><summary>PDF p.{c["source"]["page"]} · {esc(c["block_id"])} · {esc(", ".join(c["reasons"]))}</summary><pre>{esc(c["original_text"])}</pre>')
        if c['proposed_text']:out.append(f'<p>기존 미확정 교정 제안: {esc(c["proposed_text"])}</p>')
        out.append(f'<small>상태: {esc(c["current_review_status"])} · 좌표: {esc(c["source"]["bbox"])}</small></details>')
    out.append('<h2 id="search">로컬 BM25 검색 · 생성 답변 아님</h2>')
    out.append(f'<p>질문: {esc(query["question"])} · 미검수 포함: {esc(query["include_unreviewed"])} · 상태: {esc(query["status"])}</p>')
    for m in query['matches']:
        out.append(f'<article><p>{esc(m["quote"])}</p><small>PDF p.{m["source"]["page"]} · {esc(m["record_id"])} · {esc(m["status"])}</small></article>')
    out.append('<p>외부 모델·임베딩·Notion 호출 없음. 원문 이미지 SHA 확인과 문자/표/그래프 의미 검수는 구분합니다.</p></html>')
    return '\n'.join(out).encode()


def run_local(paths,*,output_dir,pdf_path=None,query='문서',include_unreviewed=False,reuse_pages=(),batch_size=8):
    paths=[Path(p) for p in paths];root=private_destination(Path(output_dir),paths+([Path(pdf_path)] if pdf_path else []))
    files=load_review_files(*paths,[],root/'unused.json')
    inputs=[sha(p.read_bytes()) for p in paths]
    audit=audit_document(files,pdf_path=pdf_path)
    plan=plan_batches(audit,batch_size=batch_size,reuse_pages=reuse_pages)
    query_result=search(files,query,include_unreviewed=include_unreviewed)
    store=LocalRunStore(root/'pages.sqlite')
    run_key=sha(encode([audit['audit_digest'],inputs]))
    page_map={p['page']:p for p in audit['pages']}
    saved=store.process(run_key,list(page_map),lambda p:page_map[p])
    if saved!=audit['pages'] or inputs!=[sha(p.read_bytes()) for p in paths]:raise ValueError('source or saved page checkpoints changed')
    payloads={'audit.json':encode(audit),'batch-plan.json':encode(plan),'search.json':encode(query_result),'index.html':render_audit(audit,query_result)}
    payloads['manifest.json']=encode({'schema_version':'1','files':{k:sha(v) for k,v in payloads.items()}})
    final=root/('local-'+sha(encode([audit['audit_digest'],plan,query_result])))
    if final.exists() or final.is_symlink():check_existing(final,payloads);return final,'unchanged'
    stage=Path(tempfile.mkdtemp(prefix='.local-audit-',dir=root))
    try:
        for name,data in payloads.items():(stage/name).write_bytes(data)
        check_existing(stage,payloads);os.rename(stage,final)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return final,'written'


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ['source','outline','review','output-dir']:p.add_argument('--'+flag,type=Path,required=True)
    p.add_argument('--pdf',type=Path);p.add_argument('--query',default='문서');p.add_argument('--include-unreviewed',action='store_true')
    p.add_argument('--reuse-page',type=int,action='append',default=[]);p.add_argument('--batch-size',type=int,default=8)
    a=p.parse_args(argv)
    try:
        final,status=run_local([a.source,a.outline,a.review],output_dir=a.output_dir,pdf_path=a.pdf,query=a.query,
                              include_unreviewed=a.include_unreviewed,reuse_pages=a.reuse_page,batch_size=a.batch_size)
        print(json.dumps({'output_path':str(final),'status':status,'model_calls':0,'embedding_calls':0,'key_reads':0,'notion_calls':0}))
    except Exception as e:p.exit(1,'local_document_blocked:'+type(e).__name__+'\n')


if __name__=='__main__':main()
