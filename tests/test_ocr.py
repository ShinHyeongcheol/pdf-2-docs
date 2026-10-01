from pdf_notion_mvp.workflow import PIPELINE_VERSION
from pdf_notion_mvp.api import default_workflow
from pdf_notion_mvp.contracts import Page, Status
from pdf_notion_mvp.ocr import OCRLine, OCRPage, ocr_to_ir
from pdf_notion_mvp.store import JobStore


def candidate(tmp_path, page_count=1):
    asset = tmp_path / "synthetic-raster.bin"
    asset.write_bytes(b"synthetic raster bytes; not a source document")
    pages = [Page(number=n, width=100, height=100) for n in range(1,page_count+1)]
    result = OCRPage(engine="apple-vision-r3", languages=["ko-KR","en-US"], lines=[
        OCRLine(text="합성 제목",confidence=.5,x0=.1,y0=.1,x1=.9,y1=.2),
    ])
    return ocr_to_ir("Synthetic OCR input", "0"*64, pages, {1:result}, {1:asset})


def test_ocr_coordinates_and_confidence_preserved(tmp_path):
    source = candidate(tmp_path)
    assert source.kind == "ocr_ir"
    block = source.document.blocks[0]
    assert block.source.bbox.model_dump() == dict(x0=10,y0=10,x1=90,y1=20)
    assert block.source.confidence == .5 and block.source.method == "ocr"
    assert source.document.extraction.human_review_required
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source,"a",PIPELINE_VERSION)
    result = store.run(job.job_id,default_workflow(asset_root=tmp_path))
    assert result.status == Status.READY and result.publish_plan.human_review_required
    assert result.validation.scope == "ir_preservation"
    assert result.publish_plan.operations[0].block == block


def test_partial_ocr_blocks_publishing_plan(tmp_path):
    source = candidate(tmp_path, page_count=2)
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.create(source,"a",PIPELINE_VERSION)
    result = store.run(job.job_id,default_workflow(asset_root=tmp_path))
    assert result.status == Status.REJECTED and result.publish_plan is None
    assert "extraction coverage is incomplete" in result.validation.errors[0]


def test_self_reported_pages_cannot_hide_deleted_page(tmp_path):
    import hashlib
    from pdf_notion_mvp.contracts import Box, ImageBlock, Source
    source = candidate(tmp_path,page_count=2)
    asset = tmp_path / "page-2.bin"
    asset.write_bytes(b"synthetic page two")
    source.document.blocks.append(ImageBlock(block_id="page-2-raster",asset_ref=str(asset),sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),source=Source(document_id=source.document.document_id,version=source.document.version,page=2,bbox=Box(x0=0,y0=0,x1=100,y1=100),method="raster")))
    source.document.extraction.pages_processed=[1,2]
    # Reproduce deletion while keeping the producer's self-report unchanged.
    source.document.blocks=[b for b in source.document.blocks if b.source.page != 2]
    store=JobStore(tmp_path / "jobs.sqlite")
    job=store.create(source,"a",PIPELINE_VERSION)
    result=store.run(job.job_id,default_workflow(asset_root=tmp_path))
    assert result.status == Status.REJECTED and result.publish_plan is None
    assert "actual block page coverage is incomplete" in result.validation.errors
    assert "page 2 must have exactly one source raster" in result.validation.errors


import pytest


@pytest.mark.parametrize("corruption",["missing_asset","digest","bounds","duplicate_raster","outside_root","no_root"])
def test_ocr_raster_failures_block_plan(tmp_path,corruption):
    source=candidate(tmp_path)
    raster=source.document.blocks[-1]
    if corruption=="missing_asset":
        from pathlib import Path
        Path(raster.asset_ref).unlink()
    elif corruption=="digest": raster.sha256="1"*64
    elif corruption=="bounds": raster.source.bbox.x0=1
    elif corruption=="duplicate_raster":
        duplicate=raster.model_copy(deep=True)
        duplicate.block_id="duplicate-raster"
        source.document.blocks.append(duplicate)
    store=JobStore(tmp_path / "jobs.sqlite")
    job=store.create(source,"a",PIPELINE_VERSION)
    root=None if corruption=="no_root" else tmp_path / "isolated" if corruption=="outside_root" else tmp_path
    result=store.run(job.job_id,default_workflow(asset_root=root))
    assert result.status==Status.REJECTED and result.publish_plan is None
    assert result.validation.errors


def test_ocr_cannot_spoof_synthetic_metadata(tmp_path):
    from pydantic import ValidationError
    from pdf_notion_mvp.contracts import FixtureInput
    source=candidate(tmp_path)
    source.document.extraction.engine="synthetic"
    with pytest.raises(ValidationError): FixtureInput.model_validate(source.model_dump())


def test_ocr_cannot_downgrade_to_synthetic_mode(tmp_path):
    from pydantic import ValidationError
    from pdf_notion_mvp.contracts import ExtractionInfo, FixtureInput
    source=candidate(tmp_path,page_count=2)
    source.kind="synthetic_ir"
    source.document.extraction=ExtractionInfo()
    # Existing OCR/raster provenance is forbidden even if extraction metadata was reset.
    with pytest.raises(ValidationError): FixtureInput.model_validate(source.model_dump())


def test_synthetic_mode_cannot_reference_local_asset(tmp_path):
    from pydantic import ValidationError
    from pdf_notion_mvp.contracts import ExtractionInfo, FixtureInput
    source=candidate(tmp_path)
    source.kind="synthetic_ir"
    source.document.extraction=ExtractionInfo()
    for block in source.document.blocks:
        block.source.method="synthetic"
        block.source.confidence=None
    with pytest.raises(ValidationError): FixtureInput.model_validate(source.model_dump())


@pytest.mark.parametrize("change",["delete","bytes","root"])
def test_ready_rechecks_current_assets_after_validation(tmp_path,change):
    from pathlib import Path
    source=candidate(tmp_path)
    store=JobStore(tmp_path / "jobs.sqlite")
    job=store.create(source,"a",PIPELINE_VERSION)
    workflow=default_workflow(asset_root=tmp_path)
    for _ in range(4): job=store.advance(job.job_id,workflow)
    assert job.status==Status.VALIDATED
    if change=="delete": Path(source.document.blocks[-1].asset_ref).unlink()
    elif change=="bytes": Path(source.document.blocks[-1].asset_ref).write_bytes(b"changed raster")
    elif change=="root": workflow=default_workflow(asset_root=tmp_path/"new-root")
    result=JobStore(store.path).advance(job.job_id,workflow)
    assert result.status==Status.REJECTED and result.publish_plan is None
    assert not result.validation.passed


def test_publisher_cannot_remove_ocr_human_review(tmp_path):
    from pdf_notion_mvp.adapters import FixtureExtractor,LocalKnowledgeAdapter,DryRunNotionPlanner
    from pdf_notion_mvp.workflow import Workflow
    class BadPublisher(DryRunNotionPlanner):
        def plan(self,restored,fingerprint):
            plan=super().plan(restored,fingerprint)
            plan.human_review_required=False
            return plan
    source=candidate(tmp_path)
    store=JobStore(tmp_path / "jobs.sqlite")
    job=store.create(source,"a",PIPELINE_VERSION)
    result=store.run(job.job_id,Workflow(FixtureExtractor(),LocalKnowledgeAdapter(),BadPublisher(),asset_root=tmp_path))
    assert result.status==Status.FAILED and result.publish_plan is None
    assert result.input.document.extraction.human_review_required


@pytest.mark.parametrize("change",["delete","bytes"])
def test_assets_changed_during_planning_cannot_be_ready(tmp_path,change):
    from pathlib import Path
    from pdf_notion_mvp.adapters import FixtureExtractor,LocalKnowledgeAdapter,DryRunNotionPlanner
    from pdf_notion_mvp.workflow import Workflow
    source=candidate(tmp_path)
    raster=Path(source.document.blocks[-1].asset_ref)
    class ChangingPublisher(DryRunNotionPlanner):
        def plan(self,restored,fingerprint):
            plan=super().plan(restored,fingerprint)
            if change=="delete": raster.unlink()
            else: raster.write_bytes(b"changed during planner execution")
            return plan
    store=JobStore(tmp_path / "jobs.sqlite")
    job=store.create(source,"a",PIPELINE_VERSION)
    result=store.run(job.job_id,Workflow(FixtureExtractor(),LocalKnowledgeAdapter(),ChangingPublisher(),asset_root=tmp_path))
    assert result.status==Status.REJECTED and result.publish_plan is None
    assert not result.validation.passed
