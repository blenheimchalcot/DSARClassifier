"""
Generic text extraction for ``.xml``, ``.json``, ``.htm``/``.html`` outside the
Purview-Exchange paths (which are already handled by ``substrate_hold.py``,
``teams_chat.py``, and ``html_transcript.py``).

These formats hold a mix of subject-PI-relevant content in this SharePoint
corpus: ILR (education) batch records (XML), saved LLM chat histories (JSON),
and exported tool reports (HTML).

The strategy is conservative — flatten the structured content to plain text,
then chunk on the standard max_chars / overlap_chars boundary used elsewhere
in the pipeline.
"""
import json
from pathlib import Path
from typing import Any, Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


_SOURCE_TYPE_BY_EXT = {
    ".xml": "xml_document",
    ".json": "json_document",
    ".htm": "html_document",
    ".html": "html_document",
}


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _flatten_json(value: Any, lines: list[str], prefix: str = "") -> None:
    """Walk a JSON document and emit ``key: value`` lines for leaves."""
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_json(v, lines, key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            key = f"{prefix}[{i}]" if prefix else f"[{i}]"
            _flatten_json(item, lines, key)
    elif value is None or value is True or value is False:
        lines.append(f"{prefix}: {value}")
    else:
        s = str(value).strip()
        if s:
            lines.append(f"{prefix}: {s}")


def _read_json(path: Path) -> str:
    text = _read_text(path)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        # malformed JSON — fall back to raw text so we don't silently drop content
        return text
    lines: list[str] = []
    _flatten_json(doc, lines)
    return "\n".join(lines)


def _read_xml(path: Path) -> str:
    """Walk the XML tree and emit ``<path>: text`` lines for elements with text or attrs."""
    from xml.etree import ElementTree as ET  # stdlib

    text_src = _read_text(path)
    try:
        root = ET.fromstring(text_src)
    except ET.ParseError:
        return text_src  # malformed → preserve raw

    lines: list[str] = []

    def _strip_ns(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    def _walk(elem: ET.Element, path_parts: list[str]) -> None:
        tag = _strip_ns(elem.tag)
        here = path_parts + [tag]
        if elem.attrib:
            for k, v in elem.attrib.items():
                if v and v.strip():
                    lines.append(f"{'/'.join(here)}@{_strip_ns(k)}: {v}")
        text = (elem.text or "").strip()
        if text:
            lines.append(f"{'/'.join(here)}: {text}")
        for child in elem:
            _walk(child, here)
        tail = (elem.tail or "").strip()
        if tail:
            lines.append(f"{'/'.join(path_parts)}#tail: {tail}")

    _walk(root, [])
    return "\n".join(lines)


def _read_html(path: Path) -> str:
    from bs4 import BeautifulSoup  # lazy import

    text = _read_text(path)
    soup = BeautifulSoup(text, "html.parser")
    # Drop script/style content — these are markup, not narrative
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def extract(path: Path, matcher: AliasMatcher, max_chars: int, overlap_chars: int) -> Iterator[Chunk]:
    ext = path.suffix.lower()
    if ext == ".json":
        text = _read_json(path)
    elif ext == ".xml":
        text = _read_xml(path)
    elif ext in {".htm", ".html"}:
        text = _read_html(path)
    else:
        raise ValueError(f"Unsupported generic-text extension: {ext}")

    if not text.strip():
        return

    source_type = _SOURCE_TYPE_BY_EXT[ext]
    parts = split_long(text, max_chars=max_chars, overlap_chars=overlap_chars)
    for i, part in enumerate(parts):
        item_ref = "full" if len(parts) == 1 else f"part-{i+1}"
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), item_ref),
            source_type=source_type,
            source_path=str(path),
            item_ref=item_ref,
            subject=path.stem,
            content=part,
            metadata={"extension": ext, "size_bytes": path.stat().st_size},
            alias_matched=matcher.matches(part),
        )
