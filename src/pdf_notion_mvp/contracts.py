from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Box(Contract):
    x0: float = Field(ge=0, allow_inf_nan=False)
    y0: float = Field(ge=0, allow_inf_nan=False)
    x1: float = Field(gt=0, allow_inf_nan=False)
    y1: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("bounding box must have positive area")
        return self


class Page(Contract):
    number: int = Field(ge=1)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)


class Source(Contract):
    document_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    page: int = Field(ge=1)
    bbox: Box
    method: Literal["synthetic", "ocr", "raster"] = "synthetic"
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class BaseBlock(Contract):
    block_id: str = Field(min_length=1)
    source: Source


class TextBlock(BaseBlock):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1)
    role: Literal["heading", "body"] = "body"


class ImageBlock(BaseBlock):
    kind: Literal["image"] = "image"
    asset_ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    caption: str = ""


class TableBlock(BaseBlock):
    kind: Literal["table"] = "table"
    cells: list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def rectangular(self):
        widths = {len(row) for row in self.cells}
        if 0 in widths or len(widths) != 1:
            raise ValueError("table must be nonempty and rectangular")
        return self


class CodeBlock(BaseBlock):
    kind: Literal["code"] = "code"
    code: str = Field(min_length=1)
    language: str = "text"


Block = Annotated[TextBlock | ImageBlock | TableBlock | CodeBlock, Field(discriminator="kind")]


class ExtractionInfo(Contract):
    engine: str = "synthetic"
    pages_processed: list[int] = Field(default_factory=list)
    human_review_required: bool = False
    warnings: list[str] = Field(default_factory=list)


class DocumentIR(Contract):
    schema_version: Literal["1"] = "1"
    document_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    pages: list[Page] = Field(min_length=1)
    blocks: list[Block] = Field(min_length=1)
    extraction: ExtractionInfo = Field(default_factory=ExtractionInfo)

    @model_validator(mode="after")
    def provenance(self):
        pages = {p.number: p for p in self.pages}
        if len(pages) != len(self.pages) or sorted(pages) != list(range(1, len(pages) + 1)):
            raise ValueError("pages must be unique and contiguous from 1")
        if len({b.block_id for b in self.blocks}) != len(self.blocks):
            raise ValueError("block IDs must be unique")
        processed = self.extraction.pages_processed
        if len(set(processed)) != len(processed) or any(n not in pages for n in processed):
            raise ValueError("extraction pages must be unique valid source pages")
        for block in self.blocks:
            s = block.source
            if s.document_id != self.document_id or s.version != self.version:
                raise ValueError("source document/version mismatch")
            if s.page not in pages:
                raise ValueError("source page does not exist")
            page = pages[s.page]
            if s.bbox.x1 > page.width or s.bbox.y1 > page.height:
                raise ValueError("source box exceeds page dimensions")
        return self


class FixtureInput(Contract):
    kind: Literal["synthetic_ir", "ocr_ir"] = "synthetic_ir"
    document: DocumentIR

    @model_validator(mode="after")
    def extraction_mode_matches(self):
        extraction = self.document.extraction
        if self.kind == "ocr_ir":
            if extraction.engine == "synthetic" or not extraction.human_review_required:
                raise ValueError("OCR input requires an OCR engine and human review flag")
            if any(b.source.method not in {"ocr", "raster"} for b in self.document.blocks):
                raise ValueError("OCR input blocks require OCR/raster provenance")
            if any(b.source.method == "ocr" and b.source.confidence is None for b in self.document.blocks):
                raise ValueError("OCR text requires source confidence")
        else:
            if extraction.model_dump() != ExtractionInfo().model_dump():
                raise ValueError("synthetic input cannot claim real extraction metadata")
            if any(b.source.method != "synthetic" or b.source.confidence is not None for b in self.document.blocks):
                raise ValueError("synthetic input permits only synthetic provenance")
            if any(isinstance(b, ImageBlock) and not b.asset_ref.startswith("synthetic://") for b in self.document.blocks):
                raise ValueError("synthetic images require synthetic references")
        return self


class Section(Contract):
    section_id: str
    title: str
    block_ids: list[str] = Field(min_length=1)


class Outline(Contract):
    sections: list[Section] = Field(min_length=1)


class RestoredSection(Contract):
    section_id: str
    title: str
    blocks: list[Block] = Field(min_length=1)


class RestoredDocument(Contract):
    sections: list[RestoredSection] = Field(min_length=1)


class ValidationReport(Contract):
    scope: Literal["ir_preservation"] = "ir_preservation"
    passed: bool
    checks: list[str]
    errors: list[str]


class PublishOperation(Contract):
    operation_key: str
    section_id: str
    section_title: str
    block: Block


class PublishPlan(Contract):
    mode: Literal["dry_run"] = "dry_run"
    target: None = None
    human_review_required: bool = True
    operations: list[PublishOperation]


class Status(StrEnum):
    QUEUED = "queued"
    EXTRACTED = "extracted"
    PLANNED = "planned"
    RESTORED = "restored"
    VALIDATED = "validated"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"


class Event(Contract):
    from_status: Status
    to_status: Status
    timestamp: str


class Job(Contract):
    job_id: str
    fingerprint: str
    pipeline_version: str
    status: Status = Status.QUEUED
    revision: int = 0
    input: FixtureInput
    ir: DocumentIR | None = None
    outline: Outline | None = None
    restored: RestoredDocument | None = None
    validation: ValidationReport | None = None
    publish_plan: PublishPlan | None = None
    last_success: Status = Status.QUEUED
    error: str | None = None
    history: list[Event] = Field(default_factory=list)
