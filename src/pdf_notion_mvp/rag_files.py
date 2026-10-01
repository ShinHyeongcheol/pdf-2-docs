"""Explicit private review JSON inputs; preserve the full IR and trusted raster root."""
import json
from dataclasses import dataclass
from pathlib import Path

from .contracts import FixtureInput
from .notion_quiz import SectionPage
from .review import HierarchicalOutline, ReviewLayer

MAX_INPUT_BYTES = 16 * 1024 * 1024


def read_json_input(path: Path, *, max_bytes: int = MAX_INPUT_BYTES):
    # Inputs are explicit JSON files, never a credential/config discovery route.
    if (path.suffix != ".json" or path.name.startswith(".env") or path.is_symlink() or
            any(p.is_symlink() for p in path.parents) or not path.is_file()):
        raise ValueError("regular explicit JSON input required")
    stat = path.stat()
    if stat.st_nlink != 1 or not 0 < stat.st_size <= max_bytes:
        raise ValueError("input size/link limit exceeded")
    return json.loads(path.read_text(encoding="utf-8"))


def protect_inputs(output: Path, inputs: list[Path]) -> None:
    for path in inputs:
        if output.resolve() == path.resolve() or (output.exists() and path.exists() and output.samefile(path)):
            raise ValueError("RAG output must not overwrite inputs or source assets")


@dataclass(frozen=True)
class ReviewFiles:
    source: FixtureInput
    hierarchy: HierarchicalOutline
    review: ReviewLayer
    bindings: list[SectionPage]
    asset_root: Path | None


def load_review_files(source_path: Path, outline_path: Path, review_path: Path,
                      binding_paths: list[Path], output: Path) -> ReviewFiles:
    paths = [source_path, outline_path, review_path, *binding_paths]
    protect_inputs(output, paths)
    source = FixtureInput.model_validate(read_json_input(source_path))
    hierarchy = HierarchicalOutline.model_validate(read_json_input(outline_path))
    review = ReviewLayer.model_validate(read_json_input(review_path))
    bindings = [SectionPage.model_validate(read_json_input(p, max_bytes=100_000)) for p in binding_paths]
    asset_root = source_path.resolve().parent if source.kind == "ocr_ir" else None
    if asset_root is not None:
        protect_inputs(output, [Path(b.asset_ref) for b in source.document.blocks if b.kind == "image"])
    return ReviewFiles(source, hierarchy, review, bindings, asset_root)
