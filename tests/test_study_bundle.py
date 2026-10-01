import base64
import hashlib
import json
from pathlib import Path

import pytest

from pdf_notion_mvp.rag_cli import load_fixture
from pdf_notion_mvp.review import document_digest
from pdf_notion_mvp.study_bundle import run, main

ROOT = Path(__file__).parents[1]
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jlp8AAAAASUVORK5CYII=')


@pytest.fixture
def lesson():
    return load_fixture(ROOT)


def save(root, lesson):
    root.mkdir(parents=True)
    paths = [root / (name+'.json') for name in ['source','outline','review']]
    for path, value in zip(paths, [lesson.source,lesson.hierarchy,lesson.review]):
        path.write_text(value.model_dump_json())
    return paths


def invoke(tmp, lesson, question='체크포인트'):
    paths = save(tmp/'inputs',lesson)
    before = {p:p.read_bytes() for p in paths}
    final, status = run(*paths,'rag.control',question,tmp/'private-output')
    assert all(p.read_bytes()==data for p,data in before.items())
    return paths, final, status


def test_one_section_preserves_layers_and_local_verified_results(tmp_path,lesson):
    _, final, status = invoke(tmp_path,lesson)
    data = json.loads((final/'study.json').read_text())
    assert status=='written' and data['section_id']=='rag.control'
    assert data['full_source_pages']==2 and data['full_source_blocks']==10
    assert data['source_ir_unmodified'] and data['human_review_required']
    assert not data['semantic_correctness_verified']
    assert data['local_quiz']['generation_mode']=='deterministic_cloze_rule'
    assert len(data['local_quiz']['questions'])==1
    assert data['local_answer']['retrieval_mode']=='lexical_local'
    assert data['local_answer']['provider_mode']=='mock'
    assert data['local_answer']['selected_section_ids']==['rag.control']
    candidate = next(b for b in data['blocks'] if b['correction'] and b['correction']['status']=='candidate')
    confirmed = next(b for b in data['blocks'] if b['correction'] and b['correction']['status']=='confirmed')
    assert candidate['effective_text']==candidate['original']['text']
    assert confirmed['effective_text']==confirmed['correction']['proposed_text']
    assert all(data[key]==0 for key in ['actual_key_reads','actual_model_requests','notion_requests'])
    assert (final/'index.html').is_file() and (final/'manifest.json').is_file()


def test_rerun_verifies_bytes_and_keeps_same_directory(tmp_path,lesson):
    paths, final, _ = invoke(tmp_path,lesson)
    before = {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in final.rglob('*') if p.is_file()}
    repeated, status = run(*paths,'rag.control','체크포인트',tmp_path/'private-output')
    assert repeated==final and status=='unchanged'
    assert all((p.read_bytes(),p.stat().st_mtime_ns)==data for p,data in before.items())
    manifest=json.loads((final/'manifest.json').read_text())
    assert all(hashlib.sha256((final/name).read_bytes()).hexdigest()==digest for name,digest in manifest['files'].items())


@pytest.mark.parametrize('damage',['edit','missing','extra','symlink','hardlink','directory'])
def test_modified_output_is_preserved_and_blocks_rerun(tmp_path,lesson,damage):
    paths, final, _ = invoke(tmp_path,lesson)
    path = final/'index.html'
    if damage=='edit': path.write_text('user notes')
    if damage=='missing': path.unlink()
    if damage=='extra': (final/'user-notes.txt').write_text('user notes')
    if damage=='symlink':
        path.unlink(); path.symlink_to(paths[0])
    if damage=='hardlink':
        import os
        path.unlink(); os.link(paths[0],path)
    if damage=='directory': (final/'extra').mkdir()
    before = paths[0].read_bytes()
    with pytest.raises(ValueError): run(*paths,'rag.control','체크포인트',tmp_path/'private-output')
    assert paths[0].read_bytes()==before
    if damage=='edit': assert path.read_text()=='user notes'
    assert not list((tmp_path/'private-output').glob('.study-stage-*'))


def test_changed_question_creates_new_bundle_without_replacing_old(tmp_path,lesson):
    paths, final, _ = invoke(tmp_path,lesson)
    new, status = run(*paths,'rag.control','unknown-unicorn',tmp_path/'private-output')
    assert new!=final and status=='written' and final.is_dir()
    assert json.loads((new/'study.json').read_text())['local_answer']['status']=='unknown'


@pytest.mark.parametrize('damage',['inside_inputs','inside_git','symlink_parent','file','relative'])
def test_private_destination_rejects_unsafe_roots_before_writing(tmp_path,lesson,damage):
    paths=save(tmp_path/'inputs',lesson)
    if damage=='inside_inputs': output=tmp_path/'inputs'/'output'
    if damage=='inside_git':
        (tmp_path/'.git').mkdir(); output=tmp_path/'out'
    if damage=='symlink_parent':
        (tmp_path/'alias').symlink_to(tmp_path/'inputs',target_is_directory=True);output=tmp_path/'alias'/'out'
    if damage=='file':
        output=tmp_path/'file';output.write_text('keep')
    if damage=='relative': output=Path('relative-output')
    with pytest.raises(ValueError): run(*paths,'rag.control','체크포인트',output)
    if damage=='file': assert output.read_text()=='keep'
    else: assert not output.exists()


def test_html_escapes_every_untrusted_display_field(tmp_path,lesson):
    injected='<script>alert("bad")</script>'
    lesson.hierarchy.nodes[1].title=injected
    lesson.source.document.blocks[1].text='체크포인트 '+injected
    lesson.review.source_digest=document_digest(lesson.source.document)
    _,final,_=invoke(tmp_path,lesson,question=injected)
    text=(final/'index.html').read_text()
    assert '<script' not in text and '&lt;script&gt;' in text
    assert 'Content-Security-Policy' in text and 'default-src' in text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in text
    assert '<summary>정답과 근거 인용</summary>' in text
    assert '<summary>교정 후보 제안 · 본문 미적용</summary>' in text
    assert 'src="http' not in text and '<iframe' not in text


def ocr_lesson(lesson,root):
    from pdf_notion_mvp.contracts import ExtractionInfo
    root.mkdir()
    lesson.source.kind='ocr_ir'
    lesson.source.document.extraction=ExtractionInfo(engine='authored-ocr-shape',pages_processed=[1,2],human_review_required=True)
    for block in lesson.source.document.blocks:
        if block.kind=='image':
            path=root/f'page-{block.source.page}.png';path.write_bytes(PNG)
            block.asset_ref=str(path);block.sha256=hashlib.sha256(PNG).hexdigest();block.source.method='raster'
        else:
            block.source.method='ocr';block.source.confidence=0.7
    from pdf_notion_mvp.contracts import Box
    for page in lesson.source.document.pages:
        image=next((b for b in lesson.source.document.blocks if b.kind=='image' and b.source.page==page.number),None)
        if image is None:
            image=next(b for b in lesson.source.document.blocks if b.kind=='image').model_copy(deep=True)
            image.block_id=f'authored-raster-{page.number}';image.source.page=page.number
            image.asset_ref=str(root/f'page-{page.number}.png');Path(image.asset_ref).write_bytes(PNG)
            lesson.source.document.blocks.append(image)
            next(n for n in lesson.hierarchy.nodes if n.block_ids and page.number in n.source_pages).block_ids.append(image.block_id)
        image.source.bbox=Box(x0=0,y0=0,x1=page.width,y1=page.height)
    for correction in lesson.review.corrections: correction.source.method='ocr';correction.source.confidence=0.7
    lesson.review.source_digest=document_digest(lesson.source.document)
    return lesson


def test_verified_ocr_pages_copy_exact_bytes_and_code_status(tmp_path,lesson):
    from pdf_notion_mvp.review import DerivedFragment
    lesson=ocr_lesson(lesson,tmp_path/'rasters')
    lesson.review.fragments.append(DerivedFragment(fragment_id='code-candidate',section_id='rag.control',
        source_block_ids=['rag-candidate'],kind='code',text='print("authored candidate")',status='candidate',basis='authored',correction_ids=[]))
    paths=save(tmp_path/'rasters'/'inputs',lesson)
    # The trusted root is the source JSON parent, so move inputs beside authored PNGs.
    paths2=[]
    for path in paths:
        dest=tmp_path/'rasters'/path.name;path.rename(dest);paths2.append(dest)
    final,_=run(*paths2,'rag.control','제어 입력',tmp_path/'private-output')
    data=json.loads((final/'study.json').read_text())
    assert data['fragments'][0]['status']=='candidate'
    assert data['original_pages']
    for asset in data['original_pages']:
        assert (final/asset['local_path']).read_bytes()==PNG
        assert asset['sha256']==hashlib.sha256(PNG).hexdigest()
    assert data['local_answer']['indexed_entries']==1


@pytest.mark.parametrize('damage',['changed','symlink','bad_png'])
def test_bad_raster_produces_no_bundle(tmp_path,lesson,damage):
    lesson=ocr_lesson(lesson,tmp_path/'inputs')
    image=next(b for b in lesson.source.document.blocks if b.kind=='image')
    path=Path(image.asset_ref)
    if damage=='changed': path.write_bytes(b'changed')
    if damage=='symlink':
        other=tmp_path/'other.png';other.write_bytes(PNG);path.unlink();path.symlink_to(other)
    if damage=='bad_png':
        path.write_bytes(b'authored non-PNG raster');image.sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        lesson.review.source_digest=document_digest(lesson.source.document)
    paths=[]
    for name,value in [('source',lesson.source),('outline',lesson.hierarchy),('review',lesson.review)]:
        dest=tmp_path/'inputs'/(name+'.json');dest.write_text(value.model_dump_json());paths.append(dest)
    with pytest.raises(ValueError): run(*paths,'rag.control','제어 입력',tmp_path/'private-output')
    assert not (tmp_path/'private-output').exists()


def test_no_keys_or_network_and_cli_emits_reading_path(tmp_path,lesson,monkeypatch,capsys):
    from pdf_notion_mvp import providers
    import socket
    def forbidden(*a,**kw): pytest.fail('key/model/network call forbidden')
    monkeypatch.setattr(providers,'selected_key',forbidden)
    monkeypatch.setattr(providers,'build_provider',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    old_read=Path.read_text
    def guarded(p,*a,**kw):
        assert not p.name.startswith('.env');return old_read(p,*a,**kw)
    monkeypatch.setattr(Path,'read_text',guarded)
    paths=save(tmp_path/'inputs',lesson)
    args=[]
    for name,path in zip(['source','outline','review'],paths):args += ['--'+name,str(path)]
    with pytest.raises(SystemExit) as exit:
        main(args+['--section','rag.control','--question','체크포인트','--output-dir',str(tmp_path/'private-output')])
    assert exit.value.code==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='written' and Path(result['index_html']).is_file()


def test_staging_failure_leaves_no_final_or_partial_bundle(tmp_path,lesson,monkeypatch):
    paths=save(tmp_path/'inputs',lesson)
    old_write=Path.write_bytes
    def fail_manifest(path,value):
        if path.name=='manifest.json': raise OSError('authored disk error')
        return old_write(path,value)
    monkeypatch.setattr(Path,'write_bytes',fail_manifest)
    with pytest.raises(OSError): run(*paths,'rag.control','체크포인트',tmp_path/'private-output')
    assert list((tmp_path/'private-output').iterdir())==[]
