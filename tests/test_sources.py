import sqlite3

import pytest

from dsar import db, sources


def test_source_type_is_worked_out_from_purview_metadata_and_path():
    assert sources.infer("PSTs/Exchange.001.pst", "Primary source (Purview export)", "Exchange", "") == "Purview Email"
    assert sources.infer("Files/SharePoint/HR/a.xlsx", "Primary source (Purview export)", "", "") == "Purview SharePoint"
    assert sources.infer("Files/x/a.docx", "Primary source (Purview export)", "OneDrive",
                         "https://t-my.sharepoint.com/personal/a") == "Purview OneDrive"
    assert sources.infer("Supplementary/x/a.docx", "Supplementary source: from HR", "", "") == "Ad hoc files"
    assert sources.infer("metadata/Items_0.csv", "Purview export metadata (x)", "", "") == "Purview metadata"
    assert sources.infer("odd/thing.bin", "Primary source (Purview export)", "", "") == "Other"
    with pytest.raises(ValueError):
        sources.check_source("Carrier pigeon")


def test_declared_source_wins_and_ingest_runs_are_collections():
    conn = sqlite3.connect(":memory:")
    db.init_schema(conn)
    rows = [("a", "/in/Files/SharePoint/a.xlsx", "2026-11-23T13:06:00+00:00", "Primary source (Purview export)", ""),
            ("b", "/in/Supplementary/b.csv", "2026-11-23T13:07:00+00:00", "Supplementary source: from HR (1 Dec)",
             "Database extract"),
            ("c", "/in/Supplementary/c.docx", "2026-11-23T18:36:00+00:00", "Supplementary source: from HR (1 Dec)", "")]
    for cid, path, ts, label, declared in rows:
        conn.execute("INSERT INTO chunks (chunk_id, source_type, source_path, item_ref, content, alias_matched, created_at) "
                     "VALUES (?, 'document', ?, ?, 'x', 1, ?)", (cid, path, cid, ts))
        conn.execute("INSERT INTO provenance_files (source_path, rel_path, sha256, provenance_label, recorded_at, source_type) "
                     "VALUES (?, ?, 'h', ?, ?, ?)", (path, path[4:], label, ts, declared))
    got = sources.for_chunks(conn, ["a", "b", "c"])
    assert [got[k]["source"] for k in "abc"] == ["Purview SharePoint", "Database extract", "Ad hoc files"]
    assert got["a"]["collection"] == got["b"]["collection"] == "23 Nov 13:06 · Purview export"
    assert got["c"]["collection"] == "23 Nov 18:36 · from HR"
