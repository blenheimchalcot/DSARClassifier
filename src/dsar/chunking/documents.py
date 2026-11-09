from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


SUPPORTED_EXTS = {".txt", ".md", ".docx", ".pdf"}


def extract(path: Path, matcher: AliasMatcher, max_chars: int, overlap_chars: int) -> Iterator[Chunk]:
    ext = path.suffix.lower()
    if ext in {".txt", ".md"}:
        text = path.read_text(errors="replace")
    elif ext == ".docx":
        text = _read_docx(path)
    elif ext == ".pdf":
        text = _read_pdf(path)
    else:
        raise ValueError(f"Unsupported document extension: {ext}")

    subject = path.stem
    parts = split_long(text, max_chars=max_chars, overlap_chars=overlap_chars)
    for i, part in enumerate(parts):
        item_ref = f"page-{i + 1}" if len(parts) > 1 else "full"
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), item_ref),
            source_type="document",
            source_path=str(path),
            item_ref=item_ref,
            subject=subject,
            content=part,
            metadata={"extension": ext, "size_bytes": path.stat().st_size},
            alias_matched=matcher.matches(part, subject),
        )


def _read_docx(path: Path) -> str:
    from docx import Document  # lazy import so tests without docx can still run

    # `doc.paragraphs` alone silently drops every table — and DSAR-relevant
    # documents (e.g. a Personal Data Report) often hold most of their content
    # in tables. Walk the body in document order, plus headers/footers.
    doc = Document(str(path))
    blocks = _docx_blocks(doc)
    for section in doc.sections:
        for part in (section.header, section.footer):
            if not part.is_linked_to_previous:
                blocks.extend(_docx_blocks(part))
    return "\n\n".join(b for b in blocks if b.strip())


def _docx_blocks(container) -> list[str]:
    """Paragraph text and table rows (cells joined by ' | ') in document order; recurses into nested tables."""
    from docx.table import Table

    out: list[str] = []
    for item in container.iter_inner_content():
        if isinstance(item, Table):
            for row in item.rows:
                cells: list[str] = []
                seen: set[int] = set()
                for cell in row.cells:
                    # Horizontally merged cells repeat the same underlying cell.
                    if id(cell._tc) in seen:
                        continue
                    seen.add(id(cell._tc))
                    cells.append(" / ".join(t for t in _docx_blocks(cell) if t.strip()))
                if any(c.strip() for c in cells):
                    out.append(" | ".join(cells))
        else:
            out.append(item.text)
    return out


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)
