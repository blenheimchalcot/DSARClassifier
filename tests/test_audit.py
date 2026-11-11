import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dsar import audit
from dsar.cli import main


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_chain_verifies_and_markdown_mirrors(tmp_path: Path):
    log = audit.AuditLog(tmp_path)
    log.append("note", "audit note", {"text": "case opened"})
    log.append("note", "audit note", {"text": "export received"})

    n, head, problems = log.verify()
    assert (n, problems) == (2, [])
    assert head == audit._line_hash(_lines(log.jsonl)[-1])
    md = log.md.read_text(encoding="utf-8")
    assert md.startswith("# Audit log")
    assert "case opened" in md and "entry #1, sha256" in md


def test_existing_handwritten_markdown_is_kept(tmp_path: Path):
    (tmp_path / audit.MD_NAME).write_text("# Case ABC-1\n\nOpened by hand.\n", encoding="utf-8")
    audit.AuditLog(tmp_path).append("note", "audit note", {"text": "x"})
    md = (tmp_path / audit.MD_NAME).read_text(encoding="utf-8")
    assert md.startswith("# Case ABC-1\n\nOpened by hand.\n")
    assert "# Audit log" not in md


@pytest.mark.parametrize("tamper", ["edit_middle", "delete_middle", "edit_last", "reorder"])
def test_tampering_is_detected(tmp_path: Path, tamper: str):
    log = audit.AuditLog(tmp_path)
    for i in range(3):
        log.append("note", "audit note", {"text": f"note {i}"})
    lines = _lines(log.jsonl)
    if tamper == "edit_middle":
        lines[1] = lines[1].replace("note 1", "note X")
    elif tamper == "delete_middle":
        del lines[1]
    elif tamper == "edit_last":
        lines[2] = lines[2].replace("note 2", "note X")
    else:
        lines[1], lines[2] = lines[2], lines[1]
    log.jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, _, problems = log.verify()
    assert problems


def test_run_records_success_and_failure(tmp_path: Path):
    with audit.run(tmp_path, "ingest", {"db_path": None}) as r:
        r.data["counts"] = {"email": 3}
        r.warn("1 file failed")
    with pytest.raises(RuntimeError):
        with audit.run(tmp_path, "classify", {}):
            raise RuntimeError("Ollama not reachable")

    entries = audit.AuditLog(tmp_path).entries()
    assert [(e["event"], e["command"]) for e in entries] == [
        ("start", "ingest"), ("end", "ingest"), ("start", "classify"), ("end", "classify")]
    assert entries[0]["data"]["code"]["version"]
    assert entries[1]["data"]["status"] == "ok"
    assert entries[1]["data"]["counts"] == {"email": 3}
    assert entries[1]["data"]["warnings"] == ["1 file failed"]
    assert entries[3]["data"]["status"] == "failed"
    assert "Ollama not reachable" in entries[3]["data"]["error"]
    assert entries[0]["run_id"] == entries[1]["run_id"] != entries[2]["run_id"]


def test_progress_file(tmp_path: Path):
    p = audit.Progress(tmp_path, "ingest", total=4, every_s=3600)
    assert p.update(0) is not None
    assert p.update(1) is None            # throttled
    snap = p.update(4)                    # last item always written
    assert snap["percent"] == 100.0
    assert json.loads((tmp_path / audit.PROGRESS_NAME).read_text())["done"] == 4


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


def test_pipeline_commands_write_audit_entries(tmp_path: Path):
    data = tmp_path / "data"
    (data / "input" / "Files").mkdir(parents=True)
    (data / "input" / "Files" / "note.txt").write_text("Alex Example joined the team.", encoding="utf-8")
    (data / "input" / "Files" / "photo.png").write_bytes(b"\x89PNG")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    runner = CliRunner()
    env = {"DSAR_DATA_DIR": str(data), "DSAR_OPERATOR": "test-operator"}

    for args in (["manifest", "--stage", "extracted export"],
                 ["ingest", "--config", str(config)],
                 ["provenance", "--config", str(config)],
                 ["report"],
                 ["audit", "note", "spot-checked note.txt by hand"],
                 ["audit", "verify"]):
        result = runner.invoke(main, args, env=env)
        assert result.exit_code == 0, (args, result.output)

    entries = audit.AuditLog(data / "output").entries()
    ends = {e["command"]: e["data"] for e in entries if e["event"] == "end"}
    assert set(ends) == {"manifest", "ingest", "provenance", "report"}
    assert all(d["status"] == "ok" for d in ends.values())
    assert ends["manifest"]["added"] == 2
    assert ends["ingest"]["counts"]["document"] == 1
    assert {"dsar.sqlite", "config"} <= set(ends["ingest"]["sha256"])
    assert ends["provenance"]["counts"]["manifest_match"] == 2
    assert "results.csv" in ends["report"]["sha256"]
    starts = [e for e in entries if e["event"] == "start"]
    assert all(e["actor"] == "test-operator" for e in starts)

    show = runner.invoke(main, ["audit", "show"], env=env)
    assert show.exit_code == 0 and "ingest" in show.output

    # Second manifest pass appends nothing; a changed file is refused, not re-hashed.
    (data / "input" / "Files" / "note.txt").write_text("changed", encoding="utf-8")
    result = runner.invoke(main, ["manifest"], env=env)
    assert result.exit_code != 0 and "no longer match" in result.output
    failed_end = audit.AuditLog(data / "output").entries()[-1]
    assert failed_end["data"]["status"] == "failed"
