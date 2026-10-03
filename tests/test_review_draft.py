from pathlib import Path
import pytest
from pdf_notion_mvp.review_draft import draft,main
from pdf_notion_mvp.contracts import FixtureInput
from pdf_notion_mvp.review import validate_outline


def test_page_draft_covers_every_original_block_once_without_confirmation(tmp_path):
    source=FixtureInput.model_validate_json((Path(__file__).parents[1]/'fixtures/synthetic.json').read_text())
    before=source.model_dump_json();outline,layer=draft(source)
    validate_outline(source.document,outline)
    assert all(n.review_status=='candidate' for n in outline.nodes)
    assert not layer.corrections and not layer.fragments and source.model_dump_json()==before
    assert [i for n in outline.nodes for i in n.block_ids]==[b.block_id for b in source.document.blocks]
    inp=tmp_path/'input/source.json';inp.parent.mkdir();inp.write_text(before)
    main(['--source',str(inp),'--output-dir',str(tmp_path/'review')])
    out=tmp_path/'review/outline.json';out.write_text('user edited data')
    with pytest.raises(SystemExit):main(['--source',str(inp),'--output-dir',str(tmp_path/'review')])
    assert out.read_text()=='user edited data'
