"""Draft page-based candidate review files without inferring or approving semantics."""
import argparse
from pathlib import Path
import json

from .contracts import FixtureInput
from .rag_files import read_json_input
from .review import HierarchicalOutline,OutlineNode,ReviewLayer,document_digest,validate_outline
from .study_bundle import private_destination
from .study_session import _save


def draft(source):
    source=FixtureInput.model_validate(source.model_dump());doc=source.document
    nodes=[OutlineNode(node_id='document',title=doc.name,source_pages=[p.number for p in doc.pages])]
    for page in doc.pages:
        nodes.append(OutlineNode(node_id=f'page-{page.number:04d}',parent_id='document',
            title=f'원본 p{page.number} · 절 제목 검토 필요',source_pages=[page.number],
            block_ids=[b.block_id for b in doc.blocks if b.source.page==page.number]))
    hierarchy=HierarchicalOutline(document_id=doc.document_id,version=doc.version,nodes=nodes)
    validate_outline(doc,hierarchy)
    layer=ReviewLayer(document_id=doc.document_id,version=doc.version,source_digest=document_digest(doc))
    return hierarchy,layer


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        root=private_destination(args.output_dir,[args.source]);source=FixtureInput.model_validate(read_json_input(args.source))
        hierarchy,layer=draft(source)
        _save(root/'outline.json',hierarchy.model_dump(mode='json'));_save(root/'review.json',layer.model_dump(mode='json'))
        print(json.dumps(dict(status='candidate_review_required',outline=str(root/'outline.json'),review=str(root/'review.json'),
            section_ids=[n.node_id for n in hierarchy.nodes if n.block_ids],model_requests=0,key_reads=0,
            instruction='Review every page and split/merge leaves into meaningful sections; confirm only after source comparison.'),ensure_ascii=False))
    except (ValueError,OSError) as exc:parser.exit(1,'review_draft_blocked:'+type(exc).__name__+'\n')


if __name__=='__main__':main()
