from pathlib import Path

from dsar.chunking.common import hash_chunk_id, split_long, strip_signature
from dsar.chunking.prefilter import build_matcher
from dsar.chunking import documents
from dsar.config import SubjectProfile


def _matcher():
    return build_matcher(SubjectProfile(
        name="Emma Banks",
        aliases=["Emma Banks", "EB", "Emma"],
        emails=["emmabanks12@gmail.com"],
    ))


def test_hash_chunk_id_is_deterministic_and_unique():
    a = hash_chunk_id("/foo.pst", "msg-1")
    b = hash_chunk_id("/foo.pst", "msg-1")
    c = hash_chunk_id("/foo.pst", "msg-2")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_split_long_passes_through_short_text():
    assert split_long("short", max_chars=100, overlap_chars=10) == ["short"]


def test_split_long_splits_on_paragraphs():
    para = "a" * 500
    text = "\n\n".join([para, para, para])
    parts = split_long(text, max_chars=600, overlap_chars=20)
    assert len(parts) >= 2
    for p in parts:
        assert len(p) <= 600 + 20  # overlap may nudge slightly


def test_strip_signature_removes_regards_block():
    body = "Hi team,\nThe update is ready.\n\nRegards,\nJane\nCompany Ltd"
    result = strip_signature(body)
    assert "Regards" not in result
    assert "The update is ready" in result


def test_strip_signature_preserved_if_contains_alias():
    import re
    body = "Hi team,\nAll good.\n\n--\nEmma Banks\nHR"
    preserve = re.compile(r"\bEmma Banks\b")
    result = strip_signature(body, preserve_if_matches=preserve)
    assert "Emma Banks" in result


def test_document_extractor_on_text_file(tmp_path: Path):
    f = tmp_path / "note.txt"
    f.write_text("This is a note about EB in the HR team.")
    chunks = list(documents.extract(f, _matcher(), max_chars=8000, overlap_chars=200))
    assert len(chunks) == 1
    c = chunks[0]
    assert c.source_type == "document"
    assert c.alias_matched is True
    assert "EB" in c.content


def test_document_extractor_no_alias(tmp_path: Path):
    f = tmp_path / "unrelated.txt"
    f.write_text("Quarterly budget review notes.")
    chunks = list(documents.extract(f, _matcher(), max_chars=8000, overlap_chars=200))
    assert len(chunks) == 1
    assert chunks[0].alias_matched is False


def test_xls_that_is_html_is_read_as_a_table(tmp_path: Path):
    from dsar.chunking import spreadsheets
    p = tmp_path / "report.xls"
    p.write_bytes(
        b'<head><META http-equiv="Content-Type" content="text/html; charset=ISO-8859-1"></head>'
        b"<table><tr><th>Apprentice</th><th>Status</th></tr>"
        b"<tr><td>Someone Else</td><td>Completed</td></tr>"
        b"<tr><td>Emma Banks</td><td>On &amp; off break</td></tr></table>")
    chunks = list(spreadsheets.extract(p, _matcher()))
    rows = [c for c in chunks if c.alias_matched]
    assert len(rows) == 1
    assert rows[0].content == "Apprentice | Status\nEmma Banks | On & off break"
    assert rows[0].item_ref == "table1!row3"


def test_csv_with_line_break_inside_a_quoted_cell(tmp_path: Path):
    from dsar.chunking import csv_table
    p = tmp_path / "survey.csv"
    p.write_bytes(b'Name,Comment\r\n"Emma Banks","line one\rline two"\r\nOther,x\r\n')
    rows = [c for c in csv_table.extract(p, _matcher()) if c.alias_matched]
    assert len(rows) == 1 and "line one" in rows[0].content and "line two" in rows[0].content


def test_fit_row_splits_an_oversize_row_and_keeps_every_part():
    from dsar.chunking import Chunk
    from dsar.chunking.common import fit_row
    content = "H1 | H2\n" + "x" * 9000 + " Emma Banks " + "y" * 9000
    row = Chunk(chunk_id="a", source_type="spreadsheet_row", source_path="/b.xlsx", item_ref="S!row5",
                subject="b", content=content, metadata={"row": 5}, alias_matched=True)
    parts = fit_row(row, max_chars=8000, overlap_chars=200, matcher=_matcher())
    assert len(parts) == 3
    assert all(len(p.content) <= 8000 for p in parts)
    assert [p.item_ref for p in parts] == ["S!row5#p1", "S!row5#p2", "S!row5#p3"]
    assert len({p.chunk_id for p in parts}) == 3
    assert [p.alias_matched for p in parts] == [False, True, False]
    assert fit_row(dataclasses_replace(row, content="short"), 8000, 200, _matcher())[0].chunk_id == "a"


def dataclasses_replace(c, **kw):
    import dataclasses
    return dataclasses.replace(c, **kw)


def test_xlsx_that_declares_a_wrong_dimension_is_read_in_full(tmp_path: Path):
    # Report exports often declare <dimension ref="A1"/> for a sheet of many rows.
    import re
    import zipfile
    from openpyxl import Workbook
    from dsar.chunking import spreadsheets
    wb = Workbook()
    ws = wb.active
    ws.append(["Learner", "Status"])
    for i in range(50):
        ws.append([f"Learner {i}", "On programme"])
    ws.append(["Emma Banks", "Gateway"])
    good = tmp_path / "good.xlsx"
    wb.save(good)
    bad = tmp_path / "report.xlsx"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1"', data)
            zout.writestr(item, data)
    rows = [c for c in spreadsheets.extract(bad, _matcher()) if c.alias_matched]
    assert len(rows) == 1 and rows[0].content == "Learner | Status\nEmma Banks | Gateway"
