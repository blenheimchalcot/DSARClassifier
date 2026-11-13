"""Case status for the dashboard, worked out from the audit log and progress file."""
import json
from pathlib import Path

from dsar import audit

# Pipeline stages in run order (docs/OPERATIONS.md §2), keyed by the audit command name.
STAGES = [
    ("manifest", "Hash the input (manifest)"),
    ("attachments", "Extract attachments"),
    ("ingest", "Ingest"),
    ("provenance", "Provenance"),
    ("qa ingest", "Ingest QA"),
    ("model-check", "Model check"),
    ("classify", "Classify"),
    ("verify-evidence", "Verify evidence"),
    ("qa classify", "Classification QA"),
    ("review export", "Review workbook out"),
    ("review import", "Review workbook in"),
    ("report", "Report"),
]


def stage_status(entries: list[dict], progress: dict | None) -> list[dict]:
    runs: dict[str, dict] = {}
    for e in entries:
        cmd = e.get("command")
        if e.get("event") == "start":
            runs[cmd] = {"started": e["ts"], "run_id": e.get("run_id"), "end": None, "count": runs.get(cmd, {}).get("count", 0) + 1}
        elif e.get("event") == "end" and cmd in runs and runs[cmd]["run_id"] == e.get("run_id"):
            runs[cmd]["end"] = e
    out = []
    for cmd, label in STAGES:
        r = runs.get(cmd)
        row = {"command": cmd, "label": label, "status": "not run", "when": "", "detail": "", "runs": 0}
        if r:
            row["runs"] = r["count"]
            if r["end"] is None:
                running = progress and progress.get("stage") == cmd
                row.update(status="running" if running else "no end entry", when=r["started"])
                if running:
                    row["detail"] = f"{progress.get('done')}/{progress.get('total')} ({progress.get('percent')}%)" + (
                        f", about {progress['eta_min']} min left" if progress.get("eta_min") else "")
            else:
                data = r["end"].get("data", {})
                row.update(status=data.get("status", "ok"), when=r["end"]["ts"])
                bits = []
                if data.get("warnings"):
                    bits.append(f"{len(data['warnings'])} warning(s)")
                if data.get("error"):
                    bits.append(str(data["error"])[:120])
                row["detail"] = "; ".join(bits)
        out.append(row)
    return out


def summary(output_dir: Path) -> dict:
    log = audit.AuditLog(output_dir)
    entries = log.entries() if log.jsonl.exists() else []
    progress = None
    p = Path(output_dir) / audit.PROGRESS_NAME
    if p.is_file():
        try:
            progress = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            progress = None
    n, head, problems = log.verify() if log.jsonl.exists() else (0, audit.GENESIS, ["no audit log yet"])
    recent = [e for e in entries if e.get("event") != "start"][-15:][::-1]
    return {"stages": stage_status(entries, progress), "progress": progress, "entries": n, "head": head,
            "problems": problems, "recent": recent}
