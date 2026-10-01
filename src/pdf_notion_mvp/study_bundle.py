"""One private, offline section: existing review, local cloze and lexical citation paths."""
import argparse
import hashlib
import html
import json
import os
import shutil
import tempfile
from pathlib import Path

from .local_quiz import plan_local_quiz
from .rag import RagWorkflow
from .rag_files import load_review_files
from .review import apply_review


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_destination(root: Path, inputs: list[Path]) -> Path:
    if not root.is_absolute() or root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise ValueError("absolute private output directory without symlinks required")
    resolved = root.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("output directory is a file")
    if any((p / '.git').exists() for p in [resolved, *resolved.parents]):
        raise ValueError("study output must be outside Git checkouts")
    if any(resolved.is_relative_to(p.resolve().parent) for p in inputs):
        raise ValueError("study output must be outside input directories")
    return resolved


def build_bundle(files, section_id: str, question: str):
    reviewed = apply_review(files.source, files.hierarchy, files.review, files.asset_root)
    section = next((s for s in reviewed.sections if s.section_id == section_id), None)
    if section is None:
        raise ValueError("select one outline leaf")
    node = next(n for n in files.hierarchy.nodes if n.node_id == section_id)
    quiz = plan_local_quiz(files.source, files.hierarchy, files.review, section_id, asset_root=files.asset_root)
    answer = RagWorkflow().run(files.source, files.hierarchy, files.review, question,
                              asset_root=files.asset_root, section_ids=[section_id])
    corrections = {c.block_id: c for c in files.review.corrections}
    blocks = []
    for block in section.source_blocks:
        correction = corrections.get(block.block_id)
        blocks.append(dict(original=block.model_dump(mode='json'),
                           effective_text=section.effective_text.get(block.block_id),
                           correction=correction.model_dump(mode='json') if correction else None))
    pages, assets = [], {}
    # Full page rasters can belong to an adjacent leaf; select by source page, not ownership.
    for block in files.source.document.blocks:
        if block.kind != 'image' or block.source.page not in node.source_pages:
            continue
        entry = dict(source=block.source.model_dump(mode='json'), block_id=block.block_id,
                     original_asset_ref=block.asset_ref, sha256=block.sha256, local_path=None)
        if files.asset_root is not None:
            path = Path(block.asset_ref)
            if (path.is_symlink() or any(p.is_symlink() for p in path.parents) or
                    not path.is_file() or path.stat().st_nlink != 1 or
                    not path.resolve().is_relative_to(files.asset_root.resolve())):
                raise ValueError("regular source raster inside trusted root required")
            payload = path.read_bytes()
            if sha(payload) != block.sha256 or not payload.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError("source raster must be unchanged PNG")
            name = f'assets/page-{block.source.page:04d}-{block.sha256}.png'
            entry['local_path'] = name
            assets[name] = payload
        pages.append(entry)
    bundle = dict(schema_version='1', mode='offline_section_review',
                  document_id=files.source.document.document_id, version=files.source.document.version,
                  source_digest=reviewed.source_digest,
                  review_digest=quiz.review_digest, section_id=section_id, title=section.title,
                  outline_review_status=node.review_status, selected_source_pages=node.source_pages,
                  full_source_pages=len(files.source.document.pages), full_source_blocks=len(files.source.document.blocks),
                  source_ir_unmodified=True, original_pages=pages, blocks=blocks,
                  fragments=[f.model_dump(mode='json') for f in files.review.fragments if f.section_id == section_id],
                  local_quiz=quiz.model_dump(mode='json'), local_answer=answer.model_dump(mode='json'),
                  human_review_required=True, semantic_correctness_verified=False,
                  actual_key_reads=0, actual_model_requests=0, notion_requests=0,
                  disclosures=['원본 페이지와 OCR 전사, 확정 교정, 미확정 후보를 구분합니다.',
                               '코드는 표시용 파생 자료이며 실행·기술 정확성을 검증하지 않았습니다.',
                               '문제는 로컬 빈칸 규칙, 답변은 로컬 키워드 검색과 모의 인용 조립입니다.',
                               '한 절의 읽기 묶음입니다. 전체 문서의 의미 검수·학습 품질·Notion 게시 완료를 뜻하지 않습니다.'])
    return bundle, assets


def render(bundle):
    def esc(value): return html.escape(str(value), quote=True)
    def details(label, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return f'<details><summary>{esc(label)}</summary><pre>{esc(text)}</pre></details>'
    def source_link(source):
        page = source['page']
        return f'<a href="#page-{page}">원본 p.{page}</a> · 좌표 {esc(source["bbox"])}'
    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<title>{esc(bundle["title"])} · 절 학습 검토</title>',
             '<style>body{max-width:1000px;margin:2rem auto;padding:0 1rem;font:17px/1.7 sans-serif;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f3f3;padding:1rem}img{max-width:100%}article{border-top:1px solid #bbb;padding:1rem 0}summary{cursor:pointer}small{display:block}</style>',
             f'<body><h1>{esc(bundle["title"])}</h1>',
             f'<p>절 목차 상태: {esc(bundle["outline_review_status"])} · 대상 페이지 {esc(bundle["selected_source_pages"])} · 전체 출처 {bundle["full_source_pages"]}쪽 / {bundle["full_source_blocks"]}블록</p>']
    parts += [f'<p>{esc(text)}</p>' for text in bundle['disclosures']]
    parts.append('<h2>전사와 검토</h2>')
    for entry in bundle['blocks']:
        block, correction = entry['original'], entry['correction']
        if block['kind'] == 'image': continue
        state = {'confirmed':'확정 교정', 'candidate':'교정 후보 · 본문 미적용'}[correction['status']] if correction else '교정 없음'
        parts.append(f'<article><small>{esc(block["block_id"])} · {source_link(block["source"])} · {esc(state)}</small>')
        if block['kind'] == 'text':
            parts.append(f'<p>{esc(entry["effective_text"])}</p>')
            parts.append(details('원본 OCR 전사' if block['source']['method']=='ocr' else '원본 텍스트', block['text']))
            if correction and correction['status']=='candidate':
                parts.append(details('교정 후보 제안 · 본문 미적용', correction['proposed_text']))
            if correction: parts.append(details('교정 제안과 검토 출처 · '+correction['status'], correction))
        else: parts.append(details('원본 '+block['kind']+' · 의미 미검증', block))
        parts.append('</article>')
    parts.append('<h2>파생 코드·표·텍스트 검토</h2>')
    for fragment in bundle['fragments']:
        status = {'confirmed':'확정 검토 기록', 'candidate':'미확정 후보'}[fragment['status']]
        parts.append(details('파생 '+fragment['kind']+' · '+status+' · 실행·의미 미검증', fragment['text']))
        parts.append(details('파생 자료 출처·교정 계보', fragment))
    if not bundle['fragments']: parts.append('<p>이 절의 파생 검토 자료 없음</p>')
    parts.append('<h2>로컬 규칙 문제</h2>')
    parts.append(f'<p>상태: {esc(bundle["local_quiz"]["status"])} · 모델 생성 아님 · 사람의 검토 필요</p>')
    for item in bundle['local_quiz']['questions']:
        parts.append(f'<article><p>{esc(item["question"]["question"])}</p>{source_link(item["source"])}')
        q = item['question']
        parts.append('<details><summary>정답과 근거 인용</summary>'
                     + f'<p>정답: {esc(q["answer"])}</p><blockquote>{esc(q["source_quote"])}</blockquote>'
                     + f'<p>{esc(q["explanation"])}</p></details>')
        parts.append(details('문제 출처·검토 계보', item)+'</article>')
    parts.append('<h2>로컬 인용 답변</h2>')
    answer = bundle['local_answer']
    parts.append(f'<p>질문: {esc(answer["question"])} · 상태: {esc(answer["status"])} · 키워드 검색 / 모의 인용 조립</p>')
    if answer['draft']:
        parts.append(f'<pre>{esc(answer["draft"]["answer"])}</pre>')
        parts.append(details('독립 검증된 인용 출처 · 의미 검수 아님', answer['draft']['citations']))
    else: parts.append('<p>확정 근거에서 인용 가능한 답변 없음</p>')
    parts.append('<h2>원본 페이지</h2>')
    for page in bundle['selected_source_pages']:
        parts.append(f'<article id="page-{page}"><h3>원본 p.{page}</h3>')
        rasters = [p for p in bundle['original_pages'] if p['source']['page']==page]
        for raster in rasters:
            if raster['local_path']:
                parts.append(f'<img src="{esc(raster["local_path"])}" alt="원본 p.{page}" loading="lazy">')
            else: parts.append('<p>합성 이미지 참조 · 실제 페이지 이미지 없음</p>')
            parts.append(details('원본 이미지 출처·SHA', raster))
        if not rasters: parts.append('<p>원본 이미지 없음</p>')
        parts.append('</article>')
    parts.append('<p><a href="study.json">전체 출처 JSON</a></p></body></html>')
    return '\n'.join(parts).encode()


def check_existing(final, payloads):
    if final.is_symlink() or not final.is_dir():
        raise ValueError("existing bundle is not a regular directory")
    expected_dirs = {final / Path(name).parent for name in payloads}
    expected_files = {final / name for name in payloads}
    for path in final.rglob('*'):
        if path.is_symlink() or path not in expected_dirs | expected_files:
            raise ValueError("existing bundle has unexpected files; human review required")
    for name, data in payloads.items():
        path = final / name
        if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != data:
            raise ValueError("existing bundle changed or incomplete; human review required")


def write_bundle(root, bundle, assets):
    data = encode(bundle)
    final = root / ('study-' + sha(data))
    payloads = {'study.json': data, 'index.html': render(bundle), **assets}
    payloads['manifest.json'] = encode({'schema_version':'1', 'files':{name:sha(value) for name,value in payloads.items()}})
    if final.exists() or final.is_symlink():
        check_existing(final, payloads)
        return final, 'unchanged'
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.study-stage-', dir=root))
    try:
        for name, value in payloads.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        check_existing(stage, payloads)
        os.rename(stage, final)
    finally:
        if stage.exists(): shutil.rmtree(stage)
    return final, 'written'


def run(source, outline, review, section, question, output_dir):
    inputs = [Path(p) for p in [source, outline, review]]
    root = private_destination(Path(output_dir), inputs)
    files = load_review_files(*inputs, [], root / 'study.json')
    bundle, assets = build_bundle(files, section, question)
    return write_bundle(root, bundle, assets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source','outline','review','section','question','output-dir']:
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(argv)
    try:
        path, status = run(args.source,args.outline,args.review,args.section,args.question,args.output_dir)
        print(json.dumps({'status':status,'index_html':str(path/'index.html'),
                          'actual_model_requests':0,'notion_requests':0}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, f'offline study bundle blocked: {exc}\n')
    parser.exit(0)


if __name__ == '__main__': main()
