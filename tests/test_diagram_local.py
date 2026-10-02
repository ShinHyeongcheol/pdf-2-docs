import base64
import hashlib
from pathlib import Path

import pytest

from pdf_notion_mvp.contracts import Box,DocumentIR,ExtractionInfo,FixtureInput,ImageBlock,Page,Source,TextBlock
from pdf_notion_mvp.diagram_local import DiagramDraft,DiagramEvidence,MockDiagramAdapter,Observation,generate_local_diagram
from pdf_notion_mvp.rag_files import ReviewFiles
from pdf_notion_mvp.review import HierarchicalOutline,OutlineNode,ReviewLayer,document_digest


@pytest.fixture
def data(tmp_path):
    p=tmp_path/'authored.png';p.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
    source=Source(document_id='authored-ocr',version='v1',page=1,bbox=Box(x0=0,y0=0,x1=100,y1=100),method='raster')
    image=ImageBlock(block_id='image',source=source,asset_ref=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    doc=DocumentIR(document_id='authored-ocr',version='v1',name='authored',pages=[Page(number=1,width=100,height=100)],blocks=[image],extraction=ExtractionInfo(engine='authored-ocr-test',pages_processed=[1],human_review_required=True))
    f=ReviewFiles(FixtureInput(kind='ocr_ir',document=doc),HierarchicalOutline(document_id=doc.document_id,version=doc.version,nodes=[OutlineNode(node_id='one',title='authored',source_pages=[1],block_ids=['image'])]),ReviewLayer(document_id=doc.document_id,version=doc.version,source_digest=document_digest(doc)),[],tmp_path)
    e=DiagramEvidence(document_id=doc.document_id,version=doc.version,source_digest=document_digest(doc),image_block_id='image',image_sha256=image.sha256,observations=[Observation(observation_id='label',kind='label',text='Authored A',bbox=Box(x0=10,y0=10,x1=20,y1=20),basis='ocr_candidate')],uncertainties=['authored local test; no actual chart reading'])
    return f,e,p


def test_real_png_boundary_mock_echo_keeps_candidates_and_no_causal_inference(data):
    f,e,_=data;a=MockDiagramAdapter();r=generate_local_diagram(f,e,a)
    assert r['status']=='ready_for_review' and r['draft']['observation_quotes']==['Authored A']
    assert r['context']['evidence']['observations'][0]['status']=='candidate' and not r['draft']['causal_inferences']
    assert a.calls==1 and not r['actual_vision_model_calls'] and not r['semantic_correctness_verified']


@pytest.mark.parametrize('change',['sha','version','bounds','duplicate','asset'])
def test_diagram_source_binding_mutations_block_before_adapter(data,change):
    f,e,p=data;a=MockDiagramAdapter()
    if change=='sha':e.image_sha256='0'*64
    elif change=='version':e.version='changed'
    elif change=='bounds':e.observations[0].bbox.x1=101
    elif change=='duplicate':e.observations.append(e.observations[0])
    elif change=='asset':p.write_bytes(p.read_bytes()+b'x')
    with pytest.raises(ValueError):generate_local_diagram(f,e,a)
    assert a.calls==0


@pytest.mark.parametrize('kind',['quote','id','inference','remote','exception'])
def test_mock_diagram_cannot_add_quotes_inferences_or_remote_calls(data,kind):
    f,e,_=data
    packet=dict(image_block_id='image',observation_ids=['label'],observation_quotes=['Authored A'])
    if kind=='quote':packet['observation_quotes']=['unsupported']
    elif kind=='id':packet['observation_ids']=['unknown']
    elif kind=='inference':packet['causal_inferences']=['invented cause']
    a=MockDiagramAdapter(RuntimeError('fabricated-secret') if kind=='exception' else packet)
    if kind=='remote':a.mode='real_remote'
    r=generate_local_diagram(f,e,a)
    assert r['status']=='failed_human_review' and r['draft'] is None
    assert 'fabricated-secret' not in str(r)
    if kind=='remote':assert a.calls==0


def test_mutable_draft_instance_cannot_bypass_inference_contract(data):
    f,e,_=data
    draft=DiagramDraft(image_block_id='image',observation_ids=['label'],observation_quotes=['Authored A'])
    draft.causal_inferences=['unsupported cause']
    r=generate_local_diagram(f,e,MockDiagramAdapter(draft))
    assert r['status']=='failed_human_review' and r['draft'] is None
