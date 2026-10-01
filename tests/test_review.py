import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdf_notion_mvp.adapters import FixtureExtractor, DryRunNotionPlanner, ProvidedHierarchyAdapter
from pdf_notion_mvp.contracts import ExtractionInfo, FixtureInput, Status
from pdf_notion_mvp.review import (HierarchicalOutline, ReviewLayer, Correction, DerivedFragment, apply_review, document_digest, validate_outline)
from pdf_notion_mvp.store import JobStore
from pdf_notion_mvp.workflow import Workflow


@pytest.fixture
def hierarchy(source):
    return HierarchicalOutline.model_validate_json((Path(__file__).parents[1] / "fixtures/synthetic-outline.json").read_text())


@pytest.fixture
def layer(source):
    return ReviewLayer(document_id=source.document.document_id, version=source.document.version, source_digest=document_digest(source.document))


def correction_source(source):
    doc = source.document.model_copy(deep=True)
    # Own synthetic visual reference covering the text; no real source data.
    image, text = doc.blocks[4], doc.blocks[1]
    image.source.bbox = text.source.bbox.model_copy(deep=True)
    correction = Correction(correction_id="fix-1", block_id=text.block_id, original_text=text.text, source=text.source,
        proposed_text="직접 작성한 교정 후보", status="candidate", basis="합성 이미지 대조", reviewer="synthetic-reviewer",
        evidence_block_id=image.block_id, evidence_bbox=text.source.bbox)
    return doc, correction


def test_hierarchy_cross_page_and_pipeline(source, hierarchy, layer, tmp_path):
    before = source.document.model_dump()
    result = apply_review(source, hierarchy, layer)
    assert len(result.sections) == 1
    assert [b.block_id for b in result.sections[0].source_blocks] == [b.block_id for b in source.document.blocks]
    assert result.original.model_dump() == before == source.document.model_dump()
    workflow = Workflow(FixtureExtractor(), ProvidedHierarchyAdapter(hierarchy), DryRunNotionPlanner())
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source, "hierarchy", workflow.pipeline_version)
    job = store.run(job.job_id, workflow)
    assert job.status == Status.READY
    assert len(job.outline.sections) == 1
    assert len(job.publish_plan.operations) == len(source.document.blocks)


@pytest.mark.parametrize("change", ["missing", "duplicate", "reorder", "unknown", "page", "version"])
def test_outline_rejects_invalid_coverage(source, hierarchy, change):
    leaf = hierarchy.nodes[1]
    if change == "missing": leaf.block_ids.pop()
    if change == "duplicate": leaf.block_ids.append(leaf.block_ids[0])
    if change == "reorder": leaf.block_ids.reverse()
    if change == "unknown": leaf.block_ids[0] = "unknown"
    if change == "page": leaf.source_pages = [1]
    if change == "version": hierarchy.version = "other"
    with pytest.raises(ValueError): validate_outline(source.document, hierarchy)


@pytest.mark.parametrize("change", ["cycle", "duplicate", "parent_blocks", "empty_leaf", "preorder"])
def test_tree_rejects_invalid_shape(source, hierarchy, change):
    nodes = hierarchy.model_dump()["nodes"]
    if change == "cycle": nodes[0]["parent_id"] = "unit.part"
    if change == "duplicate": nodes[1]["node_id"] = "unit"
    if change == "parent_blocks": nodes[0]["block_ids"] = ["b1"]
    if change == "empty_leaf": nodes[1]["block_ids"] = []
    if change == "preorder":
        nodes.append({**nodes[1], "node_id":"other-root", "parent_id":None})
        nodes.append({**nodes[1], "node_id":"late-child", "parent_id":"unit", "block_ids":["b1"]})
    with pytest.raises(ValueError):
        validate_outline(source.document, HierarchicalOutline.model_validate({**hierarchy.model_dump(), "nodes":nodes}))


def test_candidate_not_applied_confirmed_separate(source, hierarchy):
    doc, c = correction_source(source)
    layer = ReviewLayer(document_id=doc.document_id, version=doc.version, source_digest=document_digest(doc), corrections=[c])
    before = doc.model_dump()
    pending = apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer)
    assert pending.sections[0].effective_text[c.block_id] == c.original_text
    assert len(pending.sections[0].pending_corrections) == 1
    layer.corrections[0].status = "confirmed"
    confirmed = apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer)
    assert confirmed.sections[0].effective_text[c.block_id] == c.proposed_text
    assert confirmed.original.model_dump() == before == doc.model_dump()
    confirmed.sections[0].source_blocks[1].text = "mutated output"
    assert doc.model_dump() == before


@pytest.mark.parametrize("change", ["snapshot", "bbox", "page", "image", "duplicate", "digest"])
def test_correction_rejects_false_lineage(source, hierarchy, change):
    doc, c = correction_source(source)
    layer = ReviewLayer(document_id=doc.document_id, version=doc.version, source_digest=document_digest(doc), corrections=[c])
    if change == "snapshot": c.original_text = "other"
    if change == "bbox": c.source.bbox.x0 += 1
    if change == "page": c.source.page = 2
    if change == "image": c.evidence_block_id = "b3"
    if change == "duplicate": layer.corrections.append(c.model_copy(deep=True))
    if change == "digest": layer.source_digest = "0" * 64
    # Mutations happen before invocation: boundary validation must still catch them.
    with pytest.raises(ValueError): apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer)


def test_fragment_candidates_and_lineage(source, hierarchy):
    doc, c = correction_source(source)
    fragment = DerivedFragment(fragment_id="layout-1", section_id="unit.part", source_block_ids=[c.block_id], kind="text",
        text=c.proposed_text, status="candidate", basis="합성 재배치", correction_ids=[c.correction_id])
    layer = ReviewLayer(document_id=doc.document_id, version=doc.version, source_digest=document_digest(doc), corrections=[c], fragments=[fragment])
    result = apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer)
    assert not result.sections[0].confirmed_fragments
    assert result.sections[0].candidate_fragments[0].text == c.proposed_text
    layer.fragments[0].status = "confirmed"
    with pytest.raises(ValueError): apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer)
    layer.corrections[0].status = "confirmed"
    assert apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer).sections[0].confirmed_fragments
    layer.fragments[0].source_block_ids = ["unknown"]
    with pytest.raises(ValueError): apply_review(FixtureInput(kind="synthetic_ir", document=doc), hierarchy, layer)


def test_cli_prevents_input_overwrite(source, hierarchy, layer, tmp_path, monkeypatch):
    from pdf_notion_mvp.review_cli import main
    files = {"source": source, "outline": hierarchy, "layer": layer}
    for name, model in files.items(): (tmp_path / name).write_text(model.model_dump_json())
    monkeypatch.setattr("sys.argv", ["review",str(tmp_path / "source"),"--outline",str(tmp_path / "outline"),"--layer",str(tmp_path / "layer"),"--output",str(tmp_path / "source")])
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 2
    assert (tmp_path / "source").read_text() == source.model_dump_json()


def test_hierarchy_configuration_is_snapshotted_and_deduplicated(source, hierarchy, tmp_path):
    adapter = ProvidedHierarchyAdapter(hierarchy)
    first = Workflow(FixtureExtractor(), adapter, DryRunNotionPlanner())
    hierarchy.nodes[1].title = "직접 작성한 다른 목차"
    second = Workflow(FixtureExtractor(), ProvidedHierarchyAdapter(hierarchy), DryRunNotionPlanner())
    assert first.pipeline_version != second.pipeline_version
    assert adapter.plan(source.document).sections[0].title != hierarchy.nodes[1].title
    reopened = Workflow(FixtureExtractor(), ProvidedHierarchyAdapter(HierarchicalOutline.model_validate_json(adapter._hierarchy_json)), DryRunNotionPlanner())
    assert reopened.pipeline_version == first.pipeline_version
    store = JobStore(tmp_path / "jobs.sqlite")
    a = store.create(source, "same-source-a", first.pipeline_version)
    b = store.create(source, "same-source-b", second.pipeline_version)
    c = store.create(source, "reopened", reopened.pipeline_version)
    assert a.job_id != b.job_id
    assert a.job_id == c.job_id


def test_cli_prevents_hardlink_overwrite(source, hierarchy, layer, tmp_path, monkeypatch):
    import os
    from pdf_notion_mvp.review_cli import main
    for name, model in {"source":source,"outline":hierarchy,"layer":layer}.items():
        (tmp_path / name).write_text(model.model_dump_json())
    os.link(tmp_path / "source", tmp_path / "out")
    monkeypatch.setattr("sys.argv", ["review",str(tmp_path / "source"),"--outline",str(tmp_path / "outline"),"--layer",str(tmp_path / "layer"),"--output",str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exc: main()
    assert exc.value.code == 2
    assert (tmp_path / "source").read_text() == source.model_dump_json()


@pytest.mark.parametrize("claimed_mode", ["ocr_ir", "synthetic_ir"])
def test_public_review_rejects_downgraded_ocr_provenance(source, claimed_mode):
    # A coherent hierarchy/digest cannot hide OCR origin by erasing metadata/assets.
    doc = source.document.model_copy(deep=True)
    doc.blocks = [b for b in doc.blocks if b.kind == "text"]
    for block in doc.blocks:
        block.source.method = "ocr"
        block.source.confidence = 0.9
    doc.extraction = ExtractionInfo()
    hierarchy = HierarchicalOutline(document_id=doc.document_id, version=doc.version, nodes=[{
        "node_id":"whole", "title":"합성 검증", "source_pages":[1,2],
        "block_ids":[b.block_id for b in doc.blocks],
    }])
    layer = ReviewLayer(document_id=doc.document_id, version=doc.version, source_digest=document_digest(doc))
    # model_construct mimics post-construction mutation and must be revalidated.
    spoofed = FixtureInput.model_construct(kind=claimed_mode, document=doc)
    with pytest.raises(ValidationError): apply_review(spoofed, hierarchy, layer, asset_root=None)


def test_public_review_rejects_bare_document_ir(source, hierarchy, layer):
    with pytest.raises(ValidationError): apply_review(source.document, hierarchy, layer)


def test_public_review_requires_ocr_assets(source, hierarchy, tmp_path):
    doc = source.document.model_copy(deep=True)
    doc.extraction = ExtractionInfo(engine="synthetic-ocr-test", pages_processed=[1,2], human_review_required=True)
    for block in doc.blocks:
        block.source.method = "ocr"
        block.source.confidence = 0.9
    layer = ReviewLayer(document_id=doc.document_id, version=doc.version, source_digest=document_digest(doc))
    ocr = FixtureInput(kind="ocr_ir", document=doc)
    with pytest.raises(ValueError, match="raster"):
        apply_review(ocr, hierarchy, layer, asset_root=tmp_path)
