"""Credential-free connector action preparation and read-back verification.

The host executes the exact returned MCP action. No HTTP/Notion client lives here.
"""
import json
import re
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from .contracts import Contract, FixtureInput
from .notion_quiz import MARKER, SectionPage, digest, owned_key, plan_toggles, paragraph, toggle
from .quiz import QuizResult
from .review import HierarchicalOutline, ReviewLayer

SECTION_MARKER = "pdf-notion-section:v1:"
ESCAPE = set('\\*~`$[]<>{}|^#-!()')


def section_marker(binding: SectionPage) -> str:
    return SECTION_MARKER + digest([str(binding.hub_id), binding.document_id, binding.version, binding.section_id])


def encode_text(text: str) -> str:
    # Only LF has a verified literal representation in this connector subset.
    # Reject other splitlines separators rather than normalize or lose source data.
    if any(c in text for c in "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("unsupported rich-text line separator")
    return "".join("\\"+c if c in ESCAPE else c for c in text).replace("\n", "<br>")


def decode_text(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text.replace("<br>", "\n"))


def render_block(block: dict, depth: int = 0) -> str:
    kind = block["type"]
    if kind not in {"paragraph", "toggle"}:
        raise ValueError("unsupported connector block")
    data = block[kind]
    text = "".join(r["text"]["content"] for r in data["rich_text"])
    prefix = "\t" * depth
    if kind == "paragraph":
        return prefix + encode_text(text)
    children = "\n".join(render_block(child, depth+1) for child in data["children"])
    return f"{prefix}<details>\n{prefix}<summary>{encode_text(text)}</summary>\n{children}\n{prefix}</details>"


def _uuid_from_url(url: str) -> UUID:
    match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:[?/#]|$)", url)
    if not match:
        raise ValueError("missing page UUID")
    return UUID(match.group(1))


class MarkdownPart(Contract):
    cursor: str | None = None
    next_cursor: str | None = None
    has_more: bool = False
    text: str


def collect_parts(parts: list[MarkdownPart]) -> str:
    """For connector implementations exposing chunk cursors; fail closed on gaps."""
    if not parts or len(parts) > 100:
        raise ValueError("invalid snapshot parts")
    expected = None
    seen = set()
    for index, part in enumerate(parts):
        part = MarkdownPart.model_validate(part.model_dump())
        if part.cursor != expected or part.cursor in seen:
            raise ValueError("invalid cursor chain")
        seen.add(part.cursor)
        if part.has_more:
            if not part.next_cursor or index == len(parts)-1:
                raise ValueError("incomplete snapshot")
            expected = part.next_cursor
        elif part.next_cursor is not None or index != len(parts)-1:
            raise ValueError("unexpected terminal part")
    text = "".join(part.text for part in parts)
    if len(text.encode()) > 1_000_000:
        raise ValueError("snapshot exceeds local limit")
    return text


def fetch_body(packet: dict, binding: SectionPage) -> str:
    """Decode a host-provided Notion fetch result; never fetch credentials or HTTP."""
    binding = SectionPage.model_validate(binding.model_dump())
    if packet.get("isError"):
        raise ValueError("connector read failed")
    entries = packet.get("content", [])
    records = [json.loads(c["text"]) for c in entries if c.get("type") == "text"]
    if len(records) != 1:
        raise ValueError("ambiguous fetch response")
    record = records[0]
    if record.get("metadata", {}).get("type") != "page" or _uuid_from_url(record["url"]) != binding.page_id:
        raise ValueError("wrong page response")
    if record.get("truncated") is True or record.get("unknown_block_count", 0) or record.get("unknown_block_ids"):
        raise ValueError("incomplete or unknown page content")
    text = record["text"]
    if re.search(r"<unknown\b", text):
        raise ValueError("unsupported page content")
    ancestors = re.search(r"<ancestor-path>(.*?)</ancestor-path>", text, re.S)
    if not ancestors:
        raise ValueError("missing page ancestry")
    urls = re.findall(r'<parent(?:-\d+)?-page\b[^>]*url="([^"]+)"', ancestors.group(1))
    if binding.hub_id not in {_uuid_from_url(url) for url in urls}:
        raise ValueError("page outside designated hub")
    bodies = re.findall(r"<content>\n?(.*?)\n?</content>", text, re.S)
    if len(bodies) != 1 or not text.rstrip().endswith("</page>"):
        raise ValueError("incomplete page wrapper")
    body = bodies[0]
    if section_marker(binding) not in decode_text(body):
        raise ValueError("section binding marker mismatch")
    return body


def parse_owned(body: str, *, marker=MARKER) -> dict[str, dict]:
    """Parse only our strict paragraph/toggle subset; unrelated page text is kept."""
    lines = body.splitlines()
    result = {}
    index = 0
    while index < len(lines):
        if lines[index].strip() != "<details>":
            if marker in decode_text(lines[index]):
                raise ValueError("ownership marker outside app toggle")
            index += 1
            continue
        start, depth = index, 0
        while index < len(lines):
            line = lines[index].strip()
            if line == "<details>": depth += 1
            elif line == "</details>": depth -= 1
            index += 1
            if depth == 0: break
        span = lines[start:index]
        if marker not in decode_text("\n".join(span)):
            continue
        if depth:
            raise ValueError("unclosed app toggle")
        position = 0
        def parse_detail(level):
            nonlocal position
            if span[position].strip() != "<details>": raise ValueError("invalid toggle")
            position += 1
            match = re.fullmatch(r"\s*<summary>(.*?)</summary>\s*", span[position])
            if not match: raise ValueError("invalid summary")
            title = decode_text(match.group(1))
            position += 1
            children = []
            while position < len(span) and span[position].strip() != "</details>":
                line = span[position]
                if line.strip() == "<details>":
                    children.append(parse_detail(level+1))
                elif not line.strip(): position += 1
                else:
                    if not line.startswith("\t"*(level+1)):
                        raise ValueError("app content lost nesting")
                    children.append(paragraph(decode_text(line[level+1:])))
                    position += 1
            if position >= len(span): raise ValueError("missing closing toggle")
            position += 1
            return toggle(title, children)
        block = parse_detail(0)
        key = owned_key(block, marker=marker)
        if key is None or key in result or position != len(span):
            raise ValueError("duplicate or invalid app ownership")
        result[key] = block
    return result


class Checkpoint(Contract):
    page_id: UUID
    pending_keys: list[str] = Field(default_factory=list)
    expected_blocks: list[dict] = Field(default_factory=list)
    before_body: str = ""


class McpAction(Contract):
    tool: Literal["notion_update_page"] = "notion_update_page"
    arguments: dict


class PreparedPublication(Contract):
    status: Literal["append_required", "unchanged", "failed_human_review"]
    action: McpAction | None = None
    checkpoint: Checkpoint
    errors: list[str] = Field(default_factory=list)
    provider_mode: Literal["mock"] = "mock"


def _checkpoint_matches(checkpoint: Checkpoint, existing: dict[str, dict]) -> bool:
    if len(checkpoint.pending_keys) != len(checkpoint.expected_blocks):
        raise ValueError("invalid pending checkpoint")
    return all(key in existing and existing[key] == block for key, block in
        zip(checkpoint.pending_keys, checkpoint.expected_blocks))


def _require_preserved_body(body: str, checkpoint: Checkpoint) -> None:
    if not checkpoint.before_body or not body.startswith(checkpoint.before_body):
        raise ValueError("preexisting page content changed; human review required")


def prepare_publication(source: FixtureInput, hierarchy: HierarchicalOutline, layer: ReviewLayer,
                        result: QuizResult, binding: SectionPage, packet: dict,
                        checkpoint: Checkpoint | None = None, asset_root: Path | None = None) -> PreparedPublication:
    checkpoint = Checkpoint.model_validate((checkpoint or Checkpoint(page_id=binding.page_id)).model_dump())
    try:
        if checkpoint.page_id != binding.page_id: raise ValueError("checkpoint page mismatch")
        plan = plan_toggles(source, hierarchy, layer, result, binding, asset_root)
        body = fetch_body(packet, plan.binding)
        existing = parse_owned(body)
        if checkpoint.pending_keys:
            _require_preserved_body(body, checkpoint)
            if not _checkpoint_matches(checkpoint, existing):
                raise ValueError("unconfirmed prior write; do not blindly retry")
            checkpoint = Checkpoint(page_id=binding.page_id)
        for operation in plan.operations:
            if operation.operation_key in existing and existing[operation.operation_key] != operation.block:
                raise ValueError("app-owned content edited")
        missing = [o for o in plan.operations if o.operation_key not in existing]
        if not missing:
            return PreparedPublication(status="unchanged", checkpoint=checkpoint)
        content = "\n".join(render_block(o.block) for o in missing)
        if len(content.encode()) > 150_000: raise ValueError("connector action exceeds limit")
        if parse_owned(content) != {o.operation_key: o.block for o in missing}:
            raise ValueError("rendered content does not match expected blocks")
        checkpoint = Checkpoint(page_id=binding.page_id, pending_keys=[o.operation_key for o in missing],
            expected_blocks=[o.block for o in missing], before_body=body)
        return PreparedPublication(status="append_required", checkpoint=checkpoint,
            action=McpAction(arguments={"page_id":str(binding.page_id), "command":"insert_content",
                "position":{"type":"end"}, "content":content, "allow_async":False}))
    except Exception as exc:
        return PreparedPublication(status="failed_human_review", checkpoint=checkpoint,
            errors=["prepare_error:"+type(exc).__name__])


def confirm_publication(packet: dict, binding: SectionPage, checkpoint: Checkpoint) -> Checkpoint:
    checkpoint = Checkpoint.model_validate(checkpoint.model_dump())
    if checkpoint.page_id != binding.page_id: raise ValueError("wrong checkpoint page")
    body = fetch_body(packet, binding)
    _require_preserved_body(body, checkpoint)
    if not checkpoint.pending_keys or not _checkpoint_matches(checkpoint, parse_owned(body)):
        raise ValueError("write not confirmed")
    return Checkpoint(page_id=binding.page_id)


def save_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    """Host MUST save before dispatch, and retain on timeout/error/async uncertainty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+".tmp")
    temporary.write_text(checkpoint.model_dump_json(indent=2))
    temporary.replace(path)
