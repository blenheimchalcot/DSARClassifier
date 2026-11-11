import json
from pathlib import Path

from click.testing import CliRunner

from dsar import db, qa
from dsar.chunking import Chunk
from dsar.cli import main


def _chunk(path: Path, ref: str, content: str, **meta) -> Chunk:
    return Chunk(chunk_id=f"{path.name}-{ref}"[:16], source_type=meta.pop("source_type", "document"),
                 source_path=str(path), item_ref=ref, subject="s", content=content,
                 metadata=meta, alias_matched=True)


def _file(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _purview(conn, rows):
    conn.executemany(
        "INSERT INTO purview_items (row_no, status, error_detail, target_path, message_id, custodian, workload, "
        "compound_path, url, title) VALUES (?, ?, ?, ?, ?, '', ?, '', '', '')",
        [(i, *r) for i, r in enumerate(rows, start=1)])


def test_findings(tmp_path: Path):
    inp = tmp_path / "input"
    ok = _file(inp / "Files" / "ok.txt")
    noise = _file(inp / "Files" / "photo.png")
    broken = _file(inp / "Files" / "broken.docx")
    retried = _file(inp / "Files" / "retried.docx")
    orphan = _file(inp / "Files" / "orphan.txt")
    _file(inp / "Files" / ".venv" / "lib" / "x.py")
    scan = _file(inp / "Files" / "scan.pdf", b"x" * 60_000)
    pst = _file(inp / "PSTs" / "mail.pst")

    with db.connect(tmp_path / "dsar.sqlite") as conn:
        db.init_schema(conn)
        db.upsert_chunk(conn, _chunk(ok, "full", "Alex Example " * 10))
        db.upsert_chunk(conn, _chunk(ok, "big", "y" * 9000))
        db.upsert_chunk(conn, _chunk(retried, "full", "text " * 100))
        db.upsert_chunk(conn, _chunk(scan, "full", "p. 1"))
        db.upsert_chunk(conn, _chunk(pst, "msg-1", "hello", source_type="email", message_id="<a@x>"))
        db.record_skipped(conn, str(noise), "skipped: image")
        db.record_skipped(conn, str(broken), "error: File is not a zip file")
        db.record_skipped(conn, str(retried), "error: timeout")
        db.record_skipped(conn, str(inp / "Files" / ".venv"), "excluded path: '.venv' subtree (audit-bulk-skipped)")
        _purview(conn, [
            ("Succeeded", "", "Items.1.001.X.zip\\SharePoint\\missing.docx", None, "SharePoint"),
            ("Failed", "Item too large", "Items.1.001.X.zip\\SharePoint\\huge.mp4", None, "SharePoint"),
            ("Succeeded", "", "PSTs.001.zip\\mail.pst", "<a@x>", "Exchange"),
            ("Succeeded", "", "PSTs.001.zip\\mail.pst", "<b@x>", "Exchange"),
        ])
        result = qa.ingest_qa(conn, inp, max_chars=8000)

    s = result["summary"]
    by = {(f["check"], f["severity"], Path(f["path"]).name) for f in result["findings"]}
    assert ("failed", qa.FAIL, "broken.docx") in by
    assert ("failed", qa.WARN, "retried.docx") in by
    assert ("accounted", qa.FAIL, "orphan.txt") in by
    assert ("low-yield", qa.WARN, "scan.pdf") in by
    assert ("oversize", qa.FAIL, "ok.txt") in by
    assert ("purview", qa.FAIL, "missing.docx") in by
    assert ("purview", qa.WARN, "huge.mp4") in by
    assert ("purview", qa.WARN, "mail.pst") in by            # <b@x> exported but not ingested
    assert s["file_states"]["skipped (excluded folder)"] == 1
    assert s["purview"] == {"items": 4, "missing_files": 1, "export_failures": 1,
                            "pst_emails": 2, "pst_emails_not_ingested": 1}
    assert not s["passed"] and s["fail"] == 4


def test_clean_case_passes_and_gate_tracks_db(tmp_path: Path):
    inp = tmp_path / "input"
    ok = _file(inp / "Files" / "ok.txt")
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        db.init_schema(conn)
        db.upsert_chunk(conn, _chunk(ok, "full", "Alex Example joined."))
        result = qa.ingest_qa(conn, inp, max_chars=8000)
        assert result["summary"]["passed"]
        json_path, csv_path = qa.write_ingest_qa(result, tmp_path / "output")
        fp = qa.db_fingerprint(conn)
        assert qa.latest_ingest_pass(tmp_path / "output", fp) == json_path
        # Any change to the database (e.g. a re-ingest) needs a new QA run.
        db.upsert_chunk(conn, _chunk(ok, "part-2", "more"))
        assert qa.latest_ingest_pass(tmp_path / "output", qa.db_fingerprint(conn)) is None
    assert json.loads(json_path.read_text())["findings_csv"] == csv_path.name


CONFIG = """
subject:
  name: "Alex Example"
  aliases: ["Alex Example"]
  emails: []
model:
  default: "test-model"
  max_tokens: 50
  temperature: 0.0
  concurrency: 1
chunking:
  max_chars: 8000
  overlap_chars: 200
"""


def test_cli_ingest_then_qa_then_gate(tmp_path: Path):
    data = tmp_path / "data"
    _file(data / "input" / "Files" / "note.txt", b"Alex Example joined the team.")
    _file(data / "input" / "Files" / "photo.png", b"\x89PNG")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system prompt", encoding="utf-8")
    env = {"DSAR_DATA_DIR": str(data), "OLLAMA_URL": "http://127.0.0.1:9"}
    runner = CliRunner()
    for args in (["ingest", "--config", str(config)], ["provenance", "--config", str(config)],
                 ["qa", "ingest", "--config", str(config)]):
        result = runner.invoke(main, args, env=env)
        assert result.exit_code == 0, (args, result.output)
    assert "0 fail" in result.output

    # Ingest QA now passes for this DB; classify still needs a model check.
    result = runner.invoke(main, ["classify", "--config", str(config), "--system-prompt", str(prompt)], env=env)
    assert result.exit_code != 0
    assert "model-check" in result.output and "qa ingest" not in result.output
