"""
SubstrateHolds JSONs are Microsoft Planner / People Board task records exported
via Purview eDiscovery. One file = one task. The valuable PI usually sits in
`properties.notes` (HTML rich text) and `properties.title`, with audit trail in
`createdBy` / `completedBy` / dates.

We emit one chunk per file. The notes HTML is converted to plain text so the
matcher and classifier see the readable content (addresses, salaries, etc.).
"""
import json
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id
from dsar.chunking.prefilter import AliasMatcher


_FIELDS_OF_INTEREST = (
    "title", "planName", "bucketName",
    "createdBy", "completedBy",
    "createdDateTime", "completedDateTime", "dueDateTime", "startDateTime",
    "priority", "percentComplete",
)


def extract(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Bad JSON — emit a chunk recording the file so we don't silently lose it.
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), "raw"),
            source_type="substrate_record",
            source_path=str(path),
            item_ref="raw",
            subject=path.stem,
            content=f"[unparseable JSON: {exc}]",
            metadata={"parse_error": str(exc)},
            alias_matched=matcher.matches(path.stem, raw),
        )
        return

    props = (doc.get("properties") or {}) if isinstance(doc, dict) else {}
    notes_html = props.get("notes") or ""
    notes_text = _html_to_text(notes_html)

    lines: list[str] = []
    title = props.get("title") or path.stem
    if title:
        lines.append(f"Title: {title}")
    for k in _FIELDS_OF_INTEREST:
        v = props.get(k)
        if v not in (None, "", []):
            lines.append(f"{k}: {v}")
    if notes_text:
        lines.append("")
        lines.append(notes_text)

    content = "\n".join(lines).strip() or "[empty substrate record]"

    yield Chunk(
        chunk_id=hash_chunk_id(str(path), "record"),
        source_type="substrate_record",
        source_path=str(path),
        item_ref="record",
        subject=title,
        content=content,
        metadata={
            "planName": props.get("planName"),
            "bucketName": props.get("bucketName"),
            "createdBy": props.get("createdBy"),
            "completedBy": props.get("completedBy"),
            "createdDateTime": props.get("createdDateTime"),
            "completedDateTime": props.get("completedDateTime"),
        },
        alias_matched=matcher.matches(content, title),
    )


def _html_to_text(html: str) -> str:
    if not html or not html.strip():
        return ""
    from bs4 import BeautifulSoup  # lazy import
    try:
        return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()
    except Exception:
        return html
