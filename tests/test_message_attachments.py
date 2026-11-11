from email.message import EmailMessage
from pathlib import Path

from click.testing import CliRunner

from dsar import audit, db, message_attachments
from dsar.cli import main


def _email(subject: str, body: str, attachments=(), messages=()) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "hr@example.com"
    msg["To"] = "manager@example.com"
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, subtype="plain", filename=name)
    for inner in messages:
        msg.add_attachment(inner)          # message/rfc822
    return msg


def _write(path: Path, msg: EmailMessage) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(msg.as_bytes())
    return path


def _nested_case(inp: Path) -> Path:
    innermost = _email("Deepest", "deep", attachments=[("deep.txt", "Alex Example deep note")])
    inner = _email("Fwd: notes", "see attached", attachments=[("notes.txt", "Alex Example was late again.")],
                   messages=[innermost])
    outer = _email("Fwd: Fwd: notes", "fyi", attachments=[("letter.txt", "Dear Alex Example")], messages=[inner])
    return _write(inp / "Files" / "SharePoint" / "saved.eml", outer)


def test_extract_nested(tmp_path: Path):
    inp = tmp_path / "input"
    _nested_case(inp)
    rows = message_attachments.extract(message_attachments.find_messages(inp), inp)
    stored = {Path(r["stored_rel_path"]).name: r for r in rows if r.get("stored_rel_path")}
    assert {"00_letter.txt", "00_notes.txt", "00_deep.txt"} <= set(stored)
    assert stored["00_letter.txt"]["depth"] == 0 and stored["00_notes.txt"]["depth"] == 1
    assert stored["00_deep.txt"]["depth"] == 2
    assert all(r["root_rel"] == "Files/SharePoint/saved.eml" for r in rows)
    assert (inp / stored["00_notes.txt"]["stored_rel_path"]).read_text().strip() == "Alex Example was late again."
    # Extracted messages are not themselves picked up again as top-level messages.
    assert message_attachments.find_messages(inp) == [inp / "Files" / "SharePoint" / "saved.eml"]
    # Idempotent re-run.
    assert len(message_attachments.extract(message_attachments.find_messages(inp), inp)) == len(rows)


def test_depth_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(message_attachments, "MAX_DEPTH", 1)
    inp = tmp_path / "input"
    _nested_case(inp)
    rows = message_attachments.extract(message_attachments.find_messages(inp), inp)
    assert max(int(r["depth"]) for r in rows) == 0
    assert any("deeper than" in (r.get("note") or "") for r in rows)


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


def test_pipeline(tmp_path: Path):
    data = tmp_path / "data"
    inp = data / "input"
    _nested_case(inp)
    (inp / "Files" / "SharePoint" / "plan.loop").write_bytes(b"\x00loop")
    (inp / "metadata").mkdir(parents=True)
    (inp / "metadata" / "Items_0.csv").write_text(
        "Status,Target path,Custodian,Workload,Compound path\n"
        "Succeeded,Items.1.001.X.zip\\SharePoint\\saved.eml,Team Lead,SharePoint,/sites/hr/saved.eml\n",
        encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    env = {"DSAR_DATA_DIR": str(data)}
    runner = CliRunner()
    for args in (["manifest"], ["attachments"], ["ingest", "--config", str(config)],
                 ["provenance", "--config", str(config)], ["qa", "ingest", "--config", str(config)]):
        result = runner.invoke(main, args, env=env)
        assert result.exit_code == 0, (args, result.output)

    with db.connect(data / "dsar.sqlite") as conn:
        notes = conn.execute("SELECT source_path FROM chunks WHERE content LIKE '%late again%' "
                             "AND source_type = 'document'").fetchone()[0]
        custodian = conn.execute("SELECT purview_custodian FROM provenance_files WHERE source_path = ?",
                                 (notes,)).fetchone()[0]
    assert "_messages" in notes
    assert custodian == "Team Lead"                     # inherited from the saved email it came from

    entries = audit.AuditLog(data / "output").entries()
    att = next(e for e in entries if e["command"] == "attachments" and e["event"] == "end")["data"]
    assert att["saved_emails"] == 1 and att["saved_email_attachments"] >= 4
    assert message_attachments.CATALOGUE_NAME in att["sha256"]
    qa_end = [e for e in entries if e["command"] == "qa ingest" and e["event"] == "end"][-1]["data"]
    assert "manual review WARN" in qa_end["findings"]   # the Loop file
    prov = next(e for e in entries if e["command"] == "provenance" and e["event"] == "end")["data"]
    assert prov["counts"]["manifest_mismatch"] == 0 and prov["counts"]["not_in_manifest"] == 0
