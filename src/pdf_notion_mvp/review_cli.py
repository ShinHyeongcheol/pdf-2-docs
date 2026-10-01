"""Explicit file inputs only; no providers, database writes or publishing."""
import argparse
from pathlib import Path

from .contracts import FixtureInput
from .review import HierarchicalOutline, ReviewLayer, apply_review


def main():
    parser = argparse.ArgumentParser(description="Validate a hierarchy and build a separate local review layer")
    parser.add_argument("source", type=Path)
    parser.add_argument("--outline", type=Path, required=True)
    parser.add_argument("--layer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inputs = (args.source, args.outline, args.layer)
    if args.output.resolve() in {p.resolve() for p in inputs} or (args.output.exists() and any(args.output.samefile(p) for p in inputs)):
        parser.error("output must not overwrite an input")
    source = FixtureInput.model_validate_json(args.source.read_text(encoding="utf-8"))
    hierarchy = HierarchicalOutline.model_validate_json(args.outline.read_text(encoding="utf-8"))
    layer = ReviewLayer.model_validate_json(args.layer.read_text(encoding="utf-8"))
    result = apply_review(source, hierarchy, layer, args.source.resolve().parent if source.kind == "ocr_ir" else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(f"pages={len(result.original.pages)} blocks={len(result.original.blocks)} leaves={len(result.sections)} corrections={len(layer.corrections)}")
    print("checks=" + ",".join(result.checks))


if __name__ == "__main__":
    main()
