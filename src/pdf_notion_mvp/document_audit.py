"""Whole-document local coverage and a review queue, without promoting OCR candidates."""
import hashlib
import re
from collections import Counter
from pathlib import Path

from .review import apply_review
from .study_bundle import encode,sha

VERSION='document-audit-v1'
CODE=re.compile(r"\b(?:from\s+\w|import\s+\w|def\s+\w|class\s+\w|print\s*\(|invoke\s*\(|async\s+def)")
TABLE=re.compile('파라미터|매개변수|선택 기준|비교|장점|단점')
DIAGRAM=re.compile('그래프|아키텍처|다이어그램|워크플로|관계|흐름|시퀀스')


def audit_document(files, *, pdf_path=None, low_confidence=.9):
    if not 0<=low_confidence<=1: raise ValueError('confidence threshold must be between zero and one')
    reviewed=apply_review(files.source,files.hierarchy,files.review,files.asset_root)
    source=reviewed.original
    if pdf_path is not None:
        path=Path(pdf_path)
        if (path.suffix.lower()!='.pdf' or not path.is_file() or path.is_symlink() or
                any(p.is_symlink() for p in path.parents) or path.stat().st_nlink!=1):
            raise ValueError('explicit regular original PDF required')
        if sha(path.read_bytes())!=source.version: raise ValueError('original PDF version mismatch')
    blocks={b.block_id:b for b in source.blocks}
    corrections={c.block_id:c for c in reviewed.layer.corrections}
    ownership={b.block_id:s.section_id for s in reviewed.sections for b in s.source_blocks}
    leaves={s.section_id:s.title for s in reviewed.sections}
    pages=[];queue=[];figure_candidates=[]
    for page in source.pages:
        current=[b for b in source.blocks if b.source.page==page.number]
        text=[b for b in current if b.kind=='text']
        rasters=[b for b in current if b.kind=='image' and b.source.method=='raster']
        states=Counter(corrections[b.block_id].status if b.block_id in corrections else
                       'authored' if files.source.kind=='synthetic_ir' else 'unreviewed' for b in text)
        confidence=[b.source.confidence for b in text if b.source.confidence is not None]
        code_ids=[b.block_id for b in text if CODE.search(b.text)]
        table_ids=[b.block_id for b in text if TABLE.search(b.text)]
        diagram_ids=[b.block_id for b in text if DIAGRAM.search(b.text)]
        for b in text:
            c=corrections.get(b.block_id)
            reasons=[]
            if files.source.kind=='ocr_ir' and (c is None or c.status=='candidate'):reasons.append('unconfirmed_ocr')
            if b.source.confidence is not None and b.source.confidence<low_confidence:reasons.append('low_ocr_confidence')
            if b.block_id in code_ids:reasons.append('code_layout_candidate')
            if c and c.status=='candidate':reasons.append('existing_correction_candidate')
            if reasons:
                queue.append(dict(candidate_id=sha(encode([VERSION,b.block_id,reasons])),block_id=b.block_id,
                    section_id=ownership[b.block_id],source=b.source.model_dump(mode='json'),original_text=b.text,
                    proposed_text=c.proposed_text if c and c.status=='candidate' else None,
                    current_review_status=c.status if c else 'authored' if files.source.kind=='synthetic_ir' else 'unreviewed',
                    reasons=reasons,status='needs_review',automatic_confirmation=False))
        if diagram_ids:
            for image in rasters:
                figure_candidates.append(dict(candidate_id=sha(encode([VERSION,image.block_id,diagram_ids])),
                    image_block_id=image.block_id,image_sha256=image.sha256,source=image.source.model_dump(mode='json'),
                    label_block_ids=diagram_ids,status='keyword_candidate',visual_classification_verified=False,
                    inference=None,asset_ref=image.asset_ref))
        pages.append(dict(page=page.number,text_blocks=len(text),raster_blocks=len(rasters),
            section_ids=list(dict.fromkeys(ownership[b.block_id] for b in current)),review_counts=dict(states),
            min_ocr_confidence=min(confidence) if confidence else None,
            code_candidate_block_ids=code_ids,table_keyword_block_ids=table_ids,diagram_keyword_block_ids=diagram_ids,
            no_ocr_text=not text))
    result=dict(schema_version='1',pipeline_version=VERSION,document_id=source.document_id,version=source.version,
        source_digest=reviewed.source_digest,outline_digest=sha(encode(reviewed.hierarchy.model_dump(mode='json'))),
        review_digest=sha(encode(reviewed.layer.model_dump(mode='json'))),pages=pages,
        page_count=len(source.pages),block_count=len(source.blocks),leaf_count=len(leaves),
        outline_exact_block_coverage=True,extraction_full_page_coverage=files.source.kind=='synthetic_ir' or
            source.extraction.pages_processed==[p.number for p in source.pages],
        raster_assets_verified=files.source.kind in {'ocr_ir','pdf_ir'},original_pdf_sha_verified=pdf_path is not None,
        correction_queue=queue,figure_candidates=figure_candidates,
        missing_structured_kinds=[k for k in ['table','code'] if not any(b.kind==k for b in source.blocks)],
        fragment_counts=dict(Counter(f.kind+':'+f.status for f in reviewed.layer.fragments)),
        confirmed_text_blocks=sum(c.status=='confirmed' for c in reviewed.layer.corrections),
        source_unmodified=True,ocr_text_semantics_verified=False,full_document_review_complete=False,
        model_calls=0,embedding_calls=0,notion_calls=0,key_reads=0,
        limitations=['Keyword candidates do not establish actual tables, code blocks or graphs.',
                    'Raster hashes and source lineage do not verify OCR spelling, reading order or technical meaning.',
                    'Previously confirmed corrections are reused, not newly confirmed by this audit.'])
    result['audit_digest']=sha(encode(result))
    return result


def plan_batches(audit, *, batch_size=8, reuse_pages=()):
    if type(batch_size) is not int or not 1<=batch_size<=8:raise ValueError('batch size must be one to eight')
    if audit.get('audit_digest')!=sha(encode({k:v for k,v in audit.items() if k!='audit_digest'})):
        raise ValueError('audit digest mismatch')
    pages=[p['page'] for p in audit['pages']]
    if pages!=list(range(1,len(pages)+1)) or len(pages)!=audit['page_count']:raise ValueError('complete ordered page audit required')
    reuse=list(reuse_pages)
    if len(set(reuse))!=len(reuse) or not set(reuse)<=set(pages):raise ValueError('reuse pages must be unique known pages')
    selected=[p for p in pages if p not in reuse]
    batches=[dict(batch_id=sha(encode([audit['audit_digest'],selected[i:i+batch_size]])),pages=selected[i:i+batch_size],
                  status='planned',external_execution_approved=False) for i in range(0,len(selected),batch_size)]
    return dict(mode='local_batch_plan',audit_digest=audit['audit_digest'],reused_pages=sorted(reuse),batches=batches,
                all_pages_covered_exactly_once=sorted(reuse+[p for b in batches for p in b['pages']])==pages,
                model_calls=0,images_transmitted=False)
