"""Source-bound local PNG diagram observations and a closed mock explanation boundary."""
import struct
from copy import deepcopy
from typing import Literal
from pathlib import Path

from pydantic import Field

from .contracts import Box,Contract
from .review import apply_review
from .study_bundle import encode,sha


class Observation(Contract):
    observation_id:str=Field(min_length=1,max_length=80)
    kind:Literal['label','arrow','layout']
    text:str=Field(min_length=1,max_length=2000)
    bbox:Box
    basis:Literal['independent_visual_review','ocr_candidate']
    status:Literal['candidate']='candidate'


class DiagramEvidence(Contract):
    document_id:str
    version:str
    source_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    image_block_id:str
    image_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    observations:list[Observation]=Field(max_length=50)
    uncertainties:list[str]=Field(min_length=1,max_length=20)


class DiagramDraft(Contract):
    image_block_id:str
    observation_ids:list[str]=Field(max_length=50)
    observation_quotes:list[str]=Field(max_length=50)
    review_required:Literal[True]=True
    causal_inferences:list[str]=Field(max_length=0,default_factory=list)


def prepare_diagram(files,evidence):
    e=DiagramEvidence.model_validate(evidence.model_dump())
    reviewed=apply_review(files.source,files.hierarchy,files.review,files.asset_root)
    if files.source.kind!='ocr_ir':raise ValueError('real local raster evidence requires OCR IR')
    image=next((b for b in reviewed.original.blocks if b.block_id==e.image_block_id and b.kind=='image'),None)
    if (image is None or image.source.method!='raster' or
        (e.document_id,e.version,e.source_digest,e.image_sha256)!=(reviewed.original.document_id,
            reviewed.original.version,reviewed.source_digest,image.sha256)):
        raise ValueError('diagram source binding mismatch')
    path=Path(image.asset_ref)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.stat().st_nlink!=1:raise ValueError('regular source PNG required')
    data=path.read_bytes()
    if len(data)>50_000_000 or len(data)<24 or data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR':raise ValueError('bounded PNG raster required')
    width,height=struct.unpack('>II',data[16:24])
    if not 1<=width<=10000 or not 1<=height<=10000 or sha(data)!=e.image_sha256:raise ValueError('PNG dimensions or hash mismatch')
    ids=[o.observation_id for o in e.observations]
    if len(set(ids))!=len(ids):raise ValueError('unique diagram observation IDs required')
    box=image.source.bbox
    for o in e.observations:
        if not o.text.strip() or not (box.x0<=o.bbox.x0<o.bbox.x1<=box.x1 and box.y0<=o.bbox.y0<o.bbox.y1<=box.y1):
            raise ValueError('diagram observation outside source or empty')
    return dict(evidence=e.model_dump(mode='json'),source=image.source.model_dump(mode='json'),
                asset_ref=str(path),source_image_sha256=sha(data),pixel_dimensions=[width,height])


class MockDiagramAdapter:
    mode='mock_local_diagram'
    def __init__(self,response=None):self.response=response;self.calls=0
    def generate(self,context):
        self.calls+=1
        if isinstance(self.response,Exception):raise self.response
        if self.response is not None:return deepcopy(self.response)
        e=context['evidence']
        return dict(image_block_id=e['image_block_id'],observation_ids=[o['observation_id'] for o in e['observations']],
                    observation_quotes=[o['text'] for o in e['observations']])


def generate_local_diagram(files,evidence,adapter=None):
    context=prepare_diagram(files,evidence);adapter=adapter if adapter is not None else MockDiagramAdapter()
    authority=deepcopy(context)
    errors=[];draft=None
    try:
        if type(adapter) is not MockDiagramAdapter or adapter.mode!='mock_local_diagram':raise ValueError('external diagram calls are disabled in this local path')
        value=MockDiagramAdapter.generate(adapter,deepcopy(context))
        draft=DiagramDraft.model_validate(value.model_dump() if isinstance(value,DiagramDraft) else value)
        e=authority['evidence']
        if (draft.image_block_id!=e['image_block_id'] or
            draft.observation_ids!=[o['observation_id'] for o in e['observations']] or
            draft.observation_quotes!=[o['text'] for o in e['observations']]):
            errors=['unsupported_diagram_observation']
    except Exception:errors=['local_diagram_validation_failed']
    return dict(mode='mock_local_diagram',status='failed_human_review' if errors else 'ready_for_review',
        context=authority,evidence_digest=sha(encode(authority)),draft=draft.model_dump(mode='json') if draft and not errors else None,
        errors=errors,actual_vision_model_calls=0,model_calls=0,key_reads=0,notion_calls=0,
        human_review_required=True,semantic_correctness_verified=False,causal_inferences_verified=False)
