"""
Spreadsheet chunker for ``.xlsx``, ``.xlsm``, ``.xls`` (legacy CFB Excel) and
``.xlsb`` (binary Excel) workbooks.

Row-level chunking: one *structure* chunk per sheet (header + first 5 rows,
``alias_matched=False``) for reviewer browsability, plus one *row* chunk for
every data row whose joined cell text matches the alias regex.

Memory model
------------
Workbooks are streamed row-by-row to avoid materialising entire sheets in RAM.
The original implementation called ``list(ws.iter_rows(...))`` which exhausted
the 16 GB Apple Silicon dev machine on the SharePoint corpus (sheets with
100k+ rows). The streaming pattern keeps peak RSS roughly constant per file.

Format dispatch
---------------
    .xlsx / .xlsm   →  openpyxl (read_only=True, data_only=True; macros ignored in .xlsm)
    .xls            →  xlrd (only format still supported by xlrd 2.x)
    .xlsb           →  pyxlsb
"""
from pathlib import Path
from typing import Iterable, Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id
from dsar.chunking.prefilter import AliasMatcher


SUPPORTED_EXTS = {".xlsx", ".xlsm", ".xls", ".xlsb"}
PREVIEW_ROW_COUNT = 5

# Defensive guards observed on the SharePoint corpus.
#
# Some xlsx files have a phantom max_row of 1,048,576 (Excel's absolute
# maximum) because someone applied formatting at the very bottom of the sheet.
# openpyxl will then iterate a million empty rows; the alias regex runs on
# each. We stop after this many consecutive empty rows.
EMPTY_RUN_LIMIT = 5000

# A single pasted blob (e.g. a copied chat transcript into a "Notes" cell) can
# explode regex backtracking with overlapping-prefix word-boundary
# alternations. Rows longer than this are skipped with a methodology §8 note.
ROW_TEXT_MAX_CHARS = 200_000


def extract(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    ext = path.suffix.lower()
    if ext in {".xlsx", ".xlsm"}:
        yield from _extract_openpyxl(path, matcher)
    elif ext == ".xls":
        yield from _extract_xlrd(path, matcher)
    elif ext == ".xlsb":
        yield from _extract_pyxlsb(path, matcher)
    else:
        raise ValueError(f"Unsupported spreadsheet extension: {ext}")


def _row_to_text(row) -> str:
    return " | ".join("" if v is None else str(v) for v in row)


def _stream_sheet(
    sheet_name: str,
    row_iter: Iterable,
    path: Path,
    matcher: AliasMatcher,
) -> Iterator[Chunk]:
    """
    Common emit path. Takes an iterable that yields rows one at a time;
    consumes header + preview, then streams the rest. Constant memory per sheet.
    """
    it = iter(row_iter)
    try:
        header = next(it)
    except StopIteration:
        return
    header_text = _row_to_text(header)

    preview: list[tuple] = []
    for _ in range(PREVIEW_ROW_COUNT):
        try:
            preview.append(next(it))
        except StopIteration:
            break

    structural = "\n".join([header_text] + [_row_to_text(r) for r in preview])
    yield Chunk(
        chunk_id=hash_chunk_id(str(path), f"{sheet_name}!structure"),
        source_type="spreadsheet_row",
        source_path=str(path),
        item_ref=f"{sheet_name}!structure",
        subject=f"{path.stem} / {sheet_name}",
        content=structural,
        metadata={"sheet": sheet_name, "kind": "structure", "extension": path.suffix.lower()},
        alias_matched=False,
    )

    # Alias-check EVERY data row, including the preview rows. (The structural
    # chunk above is the *presentation* of the preview; alias matching still
    # has to scan every row or we'd silently miss the subject in rows 2-6.)
    def _scan(row, row_idx: int) -> Chunk | None:
        row_text = _row_to_text(row)
        if not row_text.strip():
            return None
        if len(row_text) > ROW_TEXT_MAX_CHARS:
            # Pathological cell content — pasted blob in a "Notes" cell etc.
            # Skipping per defensibility note in methodology §8.
            return None
        if not matcher.matches(row_text):
            return None
        return Chunk(
            chunk_id=hash_chunk_id(str(path), f"{sheet_name}!row{row_idx}"),
            source_type="spreadsheet_row",
            source_path=str(path),
            item_ref=f"{sheet_name}!row{row_idx}",
            subject=f"{path.stem} / {sheet_name}",
            content=f"{header_text}\n{row_text}",
            metadata={"sheet": sheet_name, "row": row_idx, "kind": "row",
                      "extension": path.suffix.lower()},
            alias_matched=True,
        )

    row_idx = 2  # first data row after the header
    for row in preview:
        chunk = _scan(row, row_idx)
        if chunk is not None:
            yield chunk
        row_idx += 1
    consecutive_empty = 0
    for row in it:
        if all(v is None for v in row):
            consecutive_empty += 1
            if consecutive_empty > EMPTY_RUN_LIMIT:
                # Phantom max_row: someone applied formatting at row 1,048,576
                # which makes openpyxl iterate the lot. Stop scanning.
                break
            row_idx += 1
            continue
        consecutive_empty = 0
        chunk = _scan(row, row_idx)
        if chunk is not None:
            yield chunk
        row_idx += 1


def _extract_openpyxl(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    from openpyxl import load_workbook  # lazy import

    wb = load_workbook(filename=str(path), data_only=True, read_only=True)
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            yield from _stream_sheet(sheet_name, ws.iter_rows(values_only=True), path, matcher)
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _extract_xlrd(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    import xlrd  # lazy import

    book = xlrd.open_workbook(str(path), on_demand=True)
    try:
        for sheet_name in book.sheet_names():
            sh = book.sheet_by_name(sheet_name)
            def _row_iter(sheet=sh):
                for r in range(sheet.nrows):
                    yield tuple(sheet.row_values(r))
            yield from _stream_sheet(sheet_name, _row_iter(), path, matcher)
            try:
                book.unload_sheet(sheet_name)
            except Exception:
                pass
    finally:
        try:
            book.release_resources()
        except Exception:
            pass


def _extract_pyxlsb(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    from pyxlsb import open_workbook  # lazy import

    with open_workbook(str(path)) as wb:
        for sheet_name in wb.sheets:
            with wb.get_sheet(sheet_name) as sh:
                def _row_iter(sheet=sh):
                    for row in sheet.rows():
                        yield tuple(cell.v if cell is not None else None for cell in row)
                yield from _stream_sheet(sheet_name, _row_iter(), path, matcher)
