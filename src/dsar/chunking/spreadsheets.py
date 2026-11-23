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
    .xls            →  xlrd (only format still supported by xlrd 2.x); an .xls
                       that is really an HTML table (Salesforce and other "export
                       to Excel" downloads) or a zipped .xlsx is read as what it is
    .xlsb           →  pyxlsb
"""
import re
from html.parser import HTMLParser
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
        kind = _sniff_xls(path)
        if kind == "html":
            yield from _extract_html_tables(path, matcher)
        elif kind == "xlsx":
            yield from _extract_openpyxl(path, matcher)
        else:
            yield from _extract_xlrd(path, matcher)
    elif ext == ".xlsb":
        yield from _extract_pyxlsb(path, matcher)
    else:
        raise ValueError(f"Unsupported spreadsheet extension: {ext}")


def _sniff_xls(path: Path) -> str:
    """What an .xls really holds: 'cfb' (legacy Excel), 'xlsx' (zip) or 'html'."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<":
        return "html"
    return "cfb"


class _TableParser(HTMLParser):
    """Collect the rows of every <table> as lists of cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            self._close_cell()
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self):
        if self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def _close_row(self):
        self._close_cell()
        if self._row is not None:
            if not self.tables:
                self.tables.append([])
            self.tables[-1].append(self._row)
        self._row = None

    def close(self):
        super().close()
        self._close_row()


def _extract_html_tables(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    raw = path.read_bytes()
    m = re.search(rb"charset=[\"']?([A-Za-z0-9_-]+)", raw[:2048])
    text = None
    for enc in ([m.group(1).decode("ascii")] if m else []) + ["utf-8", "cp1252", "latin-1"]:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    if not any(parser.tables):
        raise ValueError("HTML saved as .xls but no table rows found")
    for i, rows in enumerate(parser.tables, start=1):
        yield from _stream_sheet(f"table{i}", (tuple(r) for r in rows), path, matcher)


def _row_to_text(row) -> str:
    return " | ".join("" if v is None else str(v) for v in row)


def _stream_sheet(
    sheet_name: str,
    row_iter: Iterable,
    path: Path,
    matcher: AliasMatcher,
    extra: dict | None = None,
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
        metadata={"sheet": sheet_name, "kind": "structure", "extension": path.suffix.lower(), **(extra or {})},
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
                      "extension": path.suffix.lower(), **(extra or {})},
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
            # Read-only mode trusts the sheet's declared <dimension>. Report
            # exports (Salesforce and others) often declare "A1" for a sheet of
            # thousands of rows, so only the first cell was read. Ignore it.
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            yield from _stream_sheet(sheet_name, ws.iter_rows(values_only=True), path, matcher)
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _extract_xlrd(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    import xlrd  # lazy import

    extra = None
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except xlrd.biffh.XLRDError as exc:
        if "encrypted" not in str(exc).lower():
            raise
        # Protecting a sheet or the workbook structure makes Excel encrypt the
        # .xls with its built-in password, which Excel then opens without
        # asking. Try that password only; a real password still fails here.
        book = xlrd.open_workbook(file_contents=_decrypt_default_password(path), on_demand=True)
        extra = {"decrypted": "Excel default password (VelvetSweatshop)"}
    try:
        for sheet_name in book.sheet_names():
            sh = book.sheet_by_name(sheet_name)
            def _row_iter(sheet=sh):
                for r in range(sheet.nrows):
                    yield tuple(sheet.row_values(r))
            yield from _stream_sheet(sheet_name, _row_iter(), path, matcher, extra)
            try:
                book.unload_sheet(sheet_name)
            except Exception:
                pass
    finally:
        try:
            book.release_resources()
        except Exception:
            pass


def _decrypt_default_password(path: Path) -> bytes:
    import io
    import msoffcrypto  # lazy import

    with open(path, "rb") as fh:
        office = msoffcrypto.OfficeFile(fh)
        try:
            office.load_key(password="VelvetSweatshop")
            out = io.BytesIO()
            office.decrypt(out)
        except Exception as exc:
            raise ValueError("Workbook is encrypted with a password other than Excel's default") from exc
    return out.getvalue()


def _extract_pyxlsb(path: Path, matcher: AliasMatcher) -> Iterator[Chunk]:
    from pyxlsb import open_workbook  # lazy import

    with open_workbook(str(path)) as wb:
        for sheet_name in wb.sheets:
            with wb.get_sheet(sheet_name) as sh:
                def _row_iter(sheet=sh):
                    for row in sheet.rows():
                        yield tuple(cell.v if cell is not None else None for cell in row)
                yield from _stream_sheet(sheet_name, _row_iter(), path, matcher)
