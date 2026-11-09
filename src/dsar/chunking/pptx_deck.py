"""
PowerPoint (.pptx) chunker.

A typical Finance Weekly / Monthly deck on SharePoint contains slides with bullet
text, tables of named individuals, and speaker notes. python-pptx exposes all
three. We emit one chunk per slide so the LLM sees structured slide-level context,
and a "deck preview" chunk (title + first 3 slides) that is *not* alias-matched —
it gives a reviewer searching the chunks table a quick handle on the file.

Speaker notes are concatenated under each slide's body text, separated by a
clearly-labelled "[Speaker notes]" marker so the model can distinguish them from
the visible slide content during classification.
"""
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


def _shape_text(shape) -> list[str]:
    """Return text from a single shape: text frame, table cells, or nothing."""
    out: list[str] = []
    if getattr(shape, "has_text_frame", False) and shape.text_frame is not None:
        tf_text = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text)
        if tf_text.strip():
            out.append(tf_text)
    if getattr(shape, "has_table", False) and shape.table is not None:
        for row in shape.table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                out.append(" | ".join(cells))
    return out


def _slide_text(slide) -> str:
    parts: list[str] = []
    # Title first, if present
    title = None
    try:
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip() or None
    except Exception:
        title = None
    if title:
        parts.append(f"Title: {title}")
    for shape in slide.shapes:
        # Avoid duplicating the title via its title-placeholder shape
        if title and getattr(shape, "name", "") == "Title 1":
            continue
        parts.extend(_shape_text(shape))
    # Speaker notes
    try:
        notes_frame = slide.notes_slide.notes_text_frame if slide.has_notes_slide else None
    except Exception:
        notes_frame = None
    if notes_frame is not None and notes_frame.text.strip():
        parts.append(f"[Speaker notes]\n{notes_frame.text.strip()}")
    return "\n\n".join(parts).strip()


def extract(path: Path, matcher: AliasMatcher, max_chars: int, overlap_chars: int) -> Iterator[Chunk]:
    from pptx import Presentation  # lazy import

    prs = Presentation(str(path))
    slide_texts: list[str] = []
    for slide in prs.slides:
        slide_texts.append(_slide_text(slide))

    # Deck preview: file stem + first 3 non-empty slides, alias_matched=False
    preview_slices = [t for t in slide_texts if t][:3]
    preview = "\n\n--- slide ---\n\n".join(preview_slices) if preview_slices else "(no extractable text)"
    yield Chunk(
        chunk_id=hash_chunk_id(str(path), "deck-preview"),
        source_type="pptx_slide",
        source_path=str(path),
        item_ref="deck-preview",
        subject=path.stem,
        content=preview[:max_chars],
        metadata={"kind": "preview", "slide_count": len(slide_texts)},
        alias_matched=False,
    )

    for slide_idx, text in enumerate(slide_texts, start=1):
        if not text.strip():
            continue
        # Slides can be very large (paste-bin style content); split if needed.
        for part_idx, part in enumerate(split_long(text, max_chars=max_chars, overlap_chars=overlap_chars), start=1):
            item_ref = f"slide{slide_idx}" if part_idx == 1 and len(split_long(text, max_chars, overlap_chars)) == 1 \
                else f"slide{slide_idx}.part{part_idx}"
            yield Chunk(
                chunk_id=hash_chunk_id(str(path), item_ref),
                source_type="pptx_slide",
                source_path=str(path),
                item_ref=item_ref,
                subject=path.stem,
                content=part,
                metadata={"slide": slide_idx, "kind": "slide"},
                alias_matched=matcher.matches(part),
            )
