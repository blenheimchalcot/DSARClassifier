"""
CSV / SharePoint List export chunker.

SharePoint List exports use ``*.aspx.csv`` and ``*.csv`` with the same row shape
as the existing spreadsheets chunker (header row + data rows). Many of the larger
CSVs in the corpus are raw data exports (Rogo, HubSpot, PHR, …) where a single
data subject row contains substantial PI.

Behaviour mirrors ``spreadsheets.extract``:
  * Emit one *structure* chunk (header + first 5 rows) per file, ``alias_matched=False``.
  * Emit one *row* chunk for every data row whose joined cell text matches the alias regex.
"""
import csv
import io
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id
from dsar.chunking.prefilter import AliasMatcher


_CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_SAMPLE_BYTES = 16384


def _decode(path: Path) -> str:
    """Read the file with the first encoding that does not raise."""
    raw = path.read_bytes()
    for enc in _CANDIDATE_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # last resort: replace bad bytes so we still get text out
    return raw.decode("utf-8", errors="replace")


def _sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        class _Default(csv.excel):
            pass
        return _Default


def _row_to_text(row: list[str]) -> str:
    return " | ".join("" if v is None else str(v) for v in row)


def extract(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    text = _decode(path)
    if not text.strip():
        return
    dialect = _sniff_dialect(text[:_SAMPLE_BYTES])
    reader = csv.reader(io.StringIO(text), dialect=dialect)

    try:
        header = next(reader)
    except StopIteration:
        return
    header_text = _row_to_text(header)

    # Peek the first 5 rows for the structural chunk, then stream the rest.
    # Streaming keeps peak memory roughly constant for files like the 119 MB
    # Hive_v3_SmartVisits.csv (was eating ~500 MB peak before this).
    preview_rows: list[list[str]] = []
    for _ in range(5):
        try:
            preview_rows.append(next(reader))
        except StopIteration:
            break
    structural = "\n".join([header_text] + [_row_to_text(r) for r in preview_rows])
    yield Chunk(
        chunk_id=hash_chunk_id(str(path), "structure"),
        source_type="csv_row",
        source_path=str(path),
        item_ref="structure",
        subject=path.stem,
        content=structural,
        metadata={"kind": "structure", "columns": len(header)},
        alias_matched=False,
    )

    # Alias-check EVERY data row, including the preview rows. The structural
    # chunk above is the *presentation* of the preview; alias matching still
    # has to scan every row or we'd silently miss the subject in rows 2-6.
    def _scan(row, row_idx: int) -> Chunk | None:
        row_text = _row_to_text(row)
        if not row_text.strip():
            return None
        # Defensive cap: pathological row from a copied-blob cell would otherwise
        # blow up regex backtracking. Documented in methodology §8.
        if len(row_text) > 200_000:
            return None
        if not matcher.matches(row_text):
            return None
        return Chunk(
            chunk_id=hash_chunk_id(str(path), f"row{row_idx}"),
            source_type="csv_row",
            source_path=str(path),
            item_ref=f"row{row_idx}",
            subject=path.stem,
            content=f"{header_text}\n{row_text}",
            metadata={"row": row_idx, "kind": "row"},
            alias_matched=True,
        )

    row_idx = 2
    for row in preview_rows:
        chunk = _scan(row, row_idx)
        if chunk is not None:
            yield chunk
        row_idx += 1
    for row in reader:
        chunk = _scan(row, row_idx)
        if chunk is not None:
            yield chunk
        row_idx += 1
