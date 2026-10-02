"""Actual authored PDF bytes through native extraction, review and offline publication."""
import hashlib
import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip('pdfplumber')

from pdf_notion_mvp.api import create_app, default_workflow
from pdf_notion_mvp.contracts import FixtureInput, Status
from pdf_notion_mvp.native_pdf import extract_native_pdf
from pdf_notion_mvp.local_quiz import plan_local_quiz
from pdf_notion_mvp.openai_adapter import ScriptedQuizAdapter
from pdf_notion_mvp.quiz import QuizWorkflow, QuizBatch
from pdf_notion_mvp.review import HierarchicalOutline, OutlineNode, ReviewLayer, DerivedFragment, apply_review, document_digest
from pdf_notion_mvp.rag import RagWorkflow, prepare_index
from pdf_notion_mvp.notion_mcp import Checkpoint, prepare_publication, confirm_publication, section_marker, save_checkpoint
from pdf_notion_mvp.notion_quiz import SectionPage
from pdf_notion_mvp.store import JobStore
from pdf_notion_mvp.workflow import PIPELINE_VERSION, Workflow
from pdf_notion_mvp.adapters import LocalKnowledgeAdapter, FixtureExtractor, DryRunNotionPlanner


def write_pdf(path, pages, rotation=0):
    """Small standard PDF writer: independent known ASCII text, no new dependency."""
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>', b'',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>']
    page_ids=[]
    for rows in pages:
        page_id=len(objects)+1; content_id=page_id+1;page_ids.append(page_id)
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Rotate {rotation} /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>'.encode())
        commands=['BT /F1 10 Tf']
        for i, text in enumerate(rows):
            escaped=text.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
            commands.append(f'1 0 0 1 32 {800-i*24} Tm ({escaped}) Tj')
        stream=('\n'.join(commands)+'\nET').encode()
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'\nendstream')
    objects[1]=f'<< /Type /Pages /Count {len(pages)} /Kids [{" ".join(str(i)+" 0 R" for i in page_ids)}] >>'.encode()
    data=b'%PDF-1.4\n'; offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    offset=len(data)
    data+=f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data+=b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets[1:])
    data+=f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{offset}\n%%EOF\n'.encode()
    path.write_bytes(data)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('external networking forbidden')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setenv('LANGSMITH_TRACING','false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2','false')


@pytest.fixture
def native(tmp_path):
    pdf=tmp_path/'new-sensor.pdf'
    write_pdf(pdf,[['1. Sensor lesson','The synthetic sensor temperature is 23.5 C.',
        'The synthetic sampling interval is 250 milliseconds.',
        'Item              Value      Unit','Temperature       23.5       C','Interval          250        ms',
        'def seconds(milliseconds):','    return milliseconds / 1000'],
        ['2. Resume lesson','A durable checkpoint resumes the incomplete stage.',
         'Citations retain the PDF version, page and coordinates.']])
    source=extract_native_pdf(pdf,tmp_path/'assets')
    assert source.document.version==hashlib.sha256(pdf.read_bytes()).hexdigest()
    # Explicit review of authored line roles and table/code grouping; not inferred semantics.
    nodes=[OutlineNode(node_id=f'page-{p.number}',title=f'Page {p.number}',source_pages=[p.number],
        block_ids=[b.block_id for b in source.document.blocks if b.source.page==p.number],review_status='confirmed') for p in source.document.pages]
    hierarchy=HierarchicalOutline(document_id=source.document.document_id,version=source.document.version,nodes=nodes)
    text=[b for b in source.document.blocks if b.kind=='text' and b.source.page==1]
    fragments=[DerivedFragment(fragment_id='table-reviewed',section_id='page-1',source_block_ids=[b.block_id for b in text[3:6]],
        kind='table',text='Item | Value | Unit\nTemperature | 23.5 | C\nInterval | 250 | ms',status='confirmed',basis='Authored PDF table rows manually compared; no automatic cell recovery'),
        DerivedFragment(fragment_id='code-reviewed',section_id='page-1',source_block_ids=[b.block_id for b in text[6:8]],
        kind='code',text='def seconds(milliseconds):\n    return milliseconds / 1000',status='confirmed',basis='Authored PDF source indentation manually supplied after visual review')]
    layer=ReviewLayer(document_id=source.document.document_id,version=source.document.version,source_digest=document_digest(source.document),fragments=fragments)
    return pdf,source,hierarchy,layer


def packet(binding,body):
    return dict(content=[dict(type='text',text=json.dumps(dict(metadata=dict(type='page'),url='https://app.notion.com/p/'+binding.page_id.hex,
        text=f'<page>\n<ancestor-path>\n<parent-page url="https://app.notion.com/p/{binding.hub_id.hex}"/>\n</ancestor-path>\n<content>\n{body}\n</content>\n</page>')))],isError=False)


def test_actual_new_pdf_end_to_end(native,tmp_path):
    pdf,source,hierarchy,layer=native; original=source.model_dump_json()
    assert [b.text for b in source.document.blocks if b.kind=='text'][:3]==[
        '1. Sensor lesson','The synthetic sensor temperature is 23.5 C.','The synthetic sampling interval is 250 milliseconds.']
    assert len(source.document.pages)==2 and len(source.document.blocks)==13
    assert len({b.block_id for b in source.document.blocks})==13
    client=TestClient(create_app(tmp_path/'jobs.sqlite',default_workflow(tmp_path/'assets')))
    request=dict(request_key='new-pdf',input=source.model_dump(mode='json'))
    job=client.post('/jobs',json=request).json();states=[job['status']]
    for _ in range(5):
        response=client.post(f"/jobs/{job['job_id']}/advance",json=dict(expected_revision=job['revision']))
        assert response.status_code==200;job=response.json();states.append(job['status'])
    assert states==['queued','extracted','planned','restored','validated','ready']
    assert len(client.get(f"/jobs/{job['job_id']}/publish-plan").json()['operations'])==13
    assert client.post('/jobs',json=request).json()['job_id']==job['job_id']
    reviewed=apply_review(source,hierarchy,layer,asset_root=tmp_path/'assets')
    assert len(reviewed.sections[0].confirmed_fragments)==2
    rules=plan_local_quiz(source,hierarchy,layer,'page-1',max_questions=1,asset_root=tmp_path/'assets')
    quiz=QuizWorkflow(ScriptedQuizAdapter([QuizBatch(questions=[q.question for q in rules.questions])])).run(source,hierarchy,layer,'page-1',asset_root=tmp_path/'assets')
    index=prepare_index(source,hierarchy,layer,asset_root=tmp_path/'assets')
    assert not set(layer.fragments[1].source_block_ids).intersection(e.source_block_id for e in index.entries)
    answer=RagWorkflow().run(source,hierarchy,layer,'sensor temperature',asset_root=tmp_path/'assets')
    assert answer.status=='ready_for_review' and answer.draft.citations[0].source.version==source.document.version
    assert RagWorkflow().run(source,hierarchy,layer,'Saturn radius',asset_root=tmp_path/'assets').status=='unknown'
    b=SectionPage(hub_id='00000000-0000-4000-8000-000000000051',page_id='00000000-0000-4000-8000-000000000052',document_id=source.document.document_id,version=source.document.version,section_id='page-1')
    before='Preserve user memo\n'+section_marker(b)
    prepared=prepare_publication(source,hierarchy,layer,quiz,b,packet(b,before),asset_root=tmp_path/'assets')
    assert prepared.status=='append_required'
    save_checkpoint(tmp_path/'publication.json',prepared.checkpoint)
    restarted=Checkpoint.model_validate_json((tmp_path/'publication.json').read_text())
    assert prepare_publication(source,hierarchy,layer,quiz,b,packet(b,before),restarted,asset_root=tmp_path/'assets').action is None
    after=before+'\n'+prepared.action.arguments['content']
    assert not confirm_publication(packet(b,after),b,restarted).pending_keys
    assert prepare_publication(source,hierarchy,layer,quiz,b,packet(b,after),restarted,asset_root=tmp_path/'assets').status=='unchanged'
    assert source.model_dump_json()==original


def test_native_pdf_missing_raster_blocks_publication(native,tmp_path):
    _,source,_,_=native
    Path(next(b.asset_ref for b in source.document.blocks if b.kind=='image')).unlink()
    store=JobStore(tmp_path/'missing.sqlite');job=store.create(source,'missing',PIPELINE_VERSION)
    result=store.run(job.job_id,default_workflow(tmp_path/'assets'))
    assert result.status==Status.REJECTED and result.publish_plan is None


def test_native_pdf_failed_plan_resume_without_extract(native,tmp_path):
    _,source,_,_=native
    class Fails(LocalKnowledgeAdapter):
        def plan(self, document): raise RuntimeError('private-data')
    store=JobStore(tmp_path/'restart.sqlite');job=store.create(source,'restart',PIPELINE_VERSION)
    failed=store.run(job.job_id,Workflow(FixtureExtractor(),Fails(),DryRunNotionPlanner(),asset_root=tmp_path/'assets'))
    assert failed.status==Status.FAILED and failed.last_success==Status.EXTRACTED
    class NeverExtract:
        def extract(self, source): raise AssertionError('checkpoint must be reused')
    store.resume(job.job_id,failed.revision)
    assert store.run(job.job_id,Workflow(NeverExtract(),LocalKnowledgeAdapter(),DryRunNotionPlanner(),asset_root=tmp_path/'assets')).status==Status.READY


def test_empty_pdf_and_wrong_mode_are_rejected(tmp_path):
    path=tmp_path/'empty.pdf';write_pdf(path,[[]])
    with pytest.raises(ValueError,match='text-only'):extract_native_pdf(path,tmp_path/'assets')
    write_pdf(path,[['Rotated native text']],rotation=90)
    with pytest.raises(ValueError,match='unrotated'):extract_native_pdf(path,tmp_path/'assets')
    with pytest.raises(ValueError):FixtureInput.model_validate(dict(kind='pdf_ir',document=dict(document_id='bad',version='bad',name='bad',pages=[dict(number=1,width=1,height=1)],blocks=[])))


def test_native_cli_preserves_pdf_and_raster_outputs(native,tmp_path,monkeypatch):
    import sys
    from pdf_notion_mvp.native_pdf import main
    pdf,source,_,_=native;before=pdf.read_bytes()
    monkeypatch.setattr(sys,'argv',['native',str(pdf),'--assets',str(tmp_path/'assets'),'--output',str(pdf)])
    with pytest.raises(SystemExit) as error:main()
    assert error.value.code==2 and pdf.read_bytes()==before
    raster=Path(next(b.asset_ref for b in source.document.blocks if b.kind=='image'));before=raster.read_bytes()
    monkeypatch.setattr(sys,'argv',['native',str(pdf),'--assets',str(tmp_path/'assets'),'--output',str(raster)])
    with pytest.raises(SystemExit) as error:main()
    assert error.value.code==2 and raster.read_bytes()==before


def test_native_assets_reuse_and_refuse_symlink_hardlink_corruption(native,tmp_path):
    import os
    pdf,source,_,_=native
    raster=Path(next(b.asset_ref for b in source.document.blocks if b.kind=='image'));data=raster.read_bytes()
    again=extract_native_pdf(pdf,tmp_path/'assets')
    assert source.model_dump()==again.model_dump() and raster.read_bytes()==data
    outside=tmp_path/'outside.txt';outside.write_text('Preserve unrelated bytes')
    original=outside.read_bytes();raster.unlink();raster.symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):extract_native_pdf(pdf,tmp_path/'assets')
    assert outside.read_bytes()==original
    raster.unlink();os.link(outside,raster)
    with pytest.raises(ValueError,match='hard-linked'):extract_native_pdf(pdf,tmp_path/'assets')
    assert outside.read_bytes()==original
    raster.unlink();raster.write_bytes(b'corrupt cache')
    with pytest.raises(ValueError,match='refusing overwrite'):extract_native_pdf(pdf,tmp_path/'assets')
    assert raster.read_bytes()==b'corrupt cache'
    parent=tmp_path/'aliased-assets';parent.symlink_to(tmp_path/'assets',target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):extract_native_pdf(pdf,parent)


def test_native_pdf_file_clis_and_model_lesson_contract(native,tmp_path,monkeypatch):
    from pdf_notion_mvp.rag_files import load_review_files
    from pdf_notion_mvp.cli import main
    from pdf_notion_mvp.review_cli import main as review_main
    from pdf_notion_mvp.document_lesson import context_for, verify_draft, BatchDraft
    import sys
    _,source,hierarchy,layer=native
    inputs=[tmp_path/'source.json',tmp_path/'outline.json',tmp_path/'review.json']
    for path,value in zip(inputs,[source,hierarchy,layer]):path.write_text(value.model_dump_json(indent=2))
    files=load_review_files(*inputs,[],tmp_path/'result.json');assert files.asset_root==tmp_path
    assert apply_review(files.source,files.hierarchy,files.review,files.asset_root).original.version==source.document.version
    monkeypatch.setattr(sys,'argv',['pdf-notion',str(inputs[0]),'--db',str(tmp_path/'cli.sqlite'),'--output',str(tmp_path/'job.json')])
    with pytest.raises(SystemExit) as end:main()
    assert end.value.code==0 and json.loads((tmp_path/'job.json').read_text())['status']=='ready'
    monkeypatch.setattr(sys,'argv',['review',str(inputs[0]),'--outline',str(inputs[1]),'--layer',str(inputs[2]),'--output',str(tmp_path/'reviewed.json')])
    review_main();assert (tmp_path/'reviewed.json').is_file()
    context,diagrams=context_for(files,[1,2]);assert not diagrams
    rows=[]
    for page in (1,2):
        unit=next(u for u in context['units'] if u['source']['page']==page and ('temperature' in u['text'] if page==1 else 'checkpoint' in u['text']))
        rows.append(dict(page=page,title=f'Synthetic page {page}',notes=[dict(text=unit['text'],unit_ids=[unit['unit_id']])],
            exercises=[dict(unit_id=unit['unit_id'],answer='23.5' if page==1 else 'checkpoint',explanation='Authored mock explanation grounded in this exact page')],
            uncertainty='Mock lesson, explicitly reviewed source; no live model quality claim'))
    draft=verify_draft(context,BatchDraft(pages=rows,diagrams=[]));assert len(draft.pages)==2
    wrong=draft.model_copy(deep=True);wrong.pages[1].notes[0].unit_ids=rows[0]['notes'][0]['unit_ids']
    with pytest.raises(ValueError,match='wrong-page'):verify_draft(context,wrong)
