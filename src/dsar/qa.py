"""
Quality assurance checks between pipeline stages.

`dsar qa ingest` runs after `ingest` + `provenance` and before `classify`. It
shows that everything Purview exported was read, and read properly:

  accounted   every file under input/ ended in exactly one state: chunked,
              skipped with a reason, or failed. A file in none is a FAIL.
  failed      files whose reader raised an error (FAIL: content not seen).
  purview     every non-PST item in Purview's item report is on disk; every
              email Purview exported into a PST was ingested (by Message-ID);
              items Purview itself failed to export are listed.
  low-yield   documents that produced almost no text for their size (scanned,
              encrypted or image-only: WARN, needs a human look).
  oversize    in-scope chunks over chunking.max_chars, which Ollama would
              truncate (FAIL).

Findings go to output/qa_ingest_<time>.csv (one row each) and a summary JSON.
`dsar classify` refuses to start until the newest ingest QA for the current
database passed.
"""
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dsar import provenance, pst_attachments

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
INGEST_GLOB = "qa_ingest_*.json"

# Documents whose text should scale with file size. Below MIN_CHARS from a file
# over MIN_BYTES, the reader almost certainly missed the content.
LOW_YIELD_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".xlsb", ".msg", ".eml"}
LOW_YIELD_MIN_BYTES = 50 * 1024
LOW_YIELD_MIN_CHARS = 200
OVERSIZE_TOLERANCE = 1.05

ERROR_PREFIXES = ("error:", "pst ingest error:")
FINDINGS_LIMIT_PER_CHECK = 5000


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def db_fingerprint(conn: sqlite3.Connection) -> dict:
    """Identifies the ingest state a QA result describes; any re-ingest changes it."""
    row = conn.execute("SELECT COUNT(*), MAX(created_at), SUM(LENGTH(content)) FROM chunks").fetchone()
    skipped = conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]
    return {"chunks": row[0], "last_chunk_at": row[1], "content_chars": row[2] or 0, "skipped": skipped}


def _is_purview_failure(status: str, error_detail: str) -> bool:
    s = (status or "").lower()
    return bool(error_detail) or any(w in s for w in ("fail", "error", "skip", "partial"))


def ingest_qa(conn: sqlite3.Connection, input_dir: Path, max_chars: int,
              in_scope: Callable[[str], bool] | None = None) -> dict:
    findings: list[dict] = []
    per_check: Counter = Counter()

    def add(check: str, severity: str, path: str, detail: str) -> None:
        per_check[(check, severity)] += 1
        if per_check[(check, severity)] <= FINDINGS_LIMIT_PER_CHECK:
            findings.append({"check": check, "severity": severity, "path": path, "detail": detail})

    # --- every file accounted for -------------------------------------------------
    chunked = {r[0] for r in conn.execute("SELECT DISTINCT source_path FROM chunks")}
    skipped: dict[str, list[str]] = {}
    for path, reason in conn.execute("SELECT source_path, reason FROM skipped"):
        # A failed PST batch is recorded once with the comma-joined paths.
        for p in (path.split(",") if reason.startswith("pst ingest error") else [path]):
            skipped.setdefault(p, []).append(reason)
    # Excluded subtrees (.venv, unzipped Office packages) are recorded once, by folder.
    skipped_roots = [p for p in skipped if Path(p).is_dir()]

    states: Counter = Counter()
    skip_reasons: Counter = Counter()
    files = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.name != ".DS_Store")
    for path in files:
        sp = str(path)
        reasons = skipped.get(sp)
        errors = [r for r in reasons or [] if r.startswith(ERROR_PREFIXES)]
        if errors and sp in chunked:
            # Skip records are never removed, so an error from an earlier run stays
            # on file after a resumed ingest reads the file successfully.
            states["chunked"] += 1
            add("failed", WARN, sp, "failed on an earlier ingest, chunked on a later one: " + "; ".join(errors))
        elif errors:
            states["failed"] += 1
            add("failed", FAIL, sp, "; ".join(errors))
        elif sp in chunked:
            states["chunked"] += 1
        elif reasons:
            states["skipped"] += 1
            for r in reasons:
                skip_reasons[r.split(":", 1)[0] if r.startswith("skipped:") else r] += 1
        elif any(sp.startswith(root.rstrip("/") + "/") for root in skipped_roots):
            states["skipped (excluded folder)"] += 1
        else:
            states["unaccounted"] += 1
            add("accounted", FAIL, sp, "no chunks and no skip record: re-run `dsar ingest`")

    # --- Purview item report -------------------------------------------------------
    items = conn.execute("SELECT status, error_detail, target_path, message_id, workload FROM purview_items").fetchall()
    on_disk = {p.relative_to(input_dir).as_posix() for p in files}
    purview = {"items": len(items), "missing_files": 0, "export_failures": 0,
               "pst_emails": 0, "pst_emails_not_ingested": 0}
    if not items:
        add("purview", WARN, "", "no Purview item report loaded (input/metadata/Items_*.csv + `dsar provenance`)")
    else:
        ingested_ids: dict[str, set[str]] = {}
        for sp, mid in conn.execute(
                "SELECT DISTINCT source_path, json_extract(metadata_json, '$.message_id') FROM chunks "
                "WHERE source_type = 'email' AND source_path LIKE '%.pst'"):
            if mid:
                ingested_ids.setdefault(Path(sp).relative_to(input_dir).as_posix()
                                        if sp.startswith(str(input_dir)) else sp, set()).add(mid)
        for status, error_detail, target_path, message_id, workload in items:
            rel = provenance.target_to_rel(target_path or "")
            if _is_purview_failure(status, error_detail):
                purview["export_failures"] += 1
                add("purview", WARN, rel or target_path or "",
                    f"Purview did not export this item ({workload}): status '{status}' {error_detail}".strip())
                continue
            if rel is None:
                continue
            if rel.startswith("PSTs/") and rel.lower().endswith(".pst"):
                if not message_id:
                    continue
                purview["pst_emails"] += 1
                if message_id not in ingested_ids.get(rel, set()):
                    purview["pst_emails_not_ingested"] += 1
                    add("purview", WARN, rel, f"exported email {message_id} not found among ingested messages")
            elif rel not in on_disk:
                purview["missing_files"] += 1
                add("purview", FAIL, rel, f"in Purview's item report ({workload}) but not under input/")

    # --- documents that yielded almost no text ---------------------------------------
    chars = dict(conn.execute("SELECT source_path, SUM(LENGTH(content)) FROM chunks GROUP BY source_path"))
    for path in files:
        if path.suffix.lower() not in LOW_YIELD_EXTS or str(path) not in chars:
            continue
        size = path.stat().st_size
        if size >= LOW_YIELD_MIN_BYTES and (chars[str(path)] or 0) < LOW_YIELD_MIN_CHARS:
            add("low-yield", WARN, str(path),
                f"{size // 1024} KB file gave {chars[str(path)] or 0} characters: scanned, image-only or "
                "protected? Review by hand")

    # --- chunks Ollama would truncate ------------------------------------------------
    limit = int(max_chars * OVERSIZE_TOLERANCE)
    for chunk_id, sp, length in conn.execute(
            "SELECT chunk_id, source_path, LENGTH(content) FROM chunks "
            "WHERE alias_matched = 1 AND skipped_as_duplicate_of IS NULL AND LENGTH(content) > ?", (limit,)):
        if in_scope is None or in_scope(sp):
            add("oversize", FAIL, sp, f"chunk {chunk_id} is {length} chars (max {max_chars}): would be truncated")

    # --- statistics ------------------------------------------------------------------
    by_type = {r[0]: {"chunks": r[1], "alias_matched": r[2], "duplicates": r[3]} for r in conn.execute(
        "SELECT source_type, COUNT(*), SUM(alias_matched), SUM(skipped_as_duplicate_of IS NOT NULL) "
        "FROM chunks GROUP BY source_type ORDER BY 2 DESC")}
    attachments_dir = input_dir / pst_attachments.ATTACH_DIR

    fails = sum(n for (_, sev), n in per_check.items() if sev == FAIL)
    warns = sum(n for (_, sev), n in per_check.items() if sev == WARN)
    summary = {
        "passed": fails == 0,
        "files": len(files),
        "file_states": dict(states),
        "skip_reasons": dict(skip_reasons.most_common()),
        "purview": purview,
        "by_source_type": by_type,
        "pst_attachments_extracted": attachments_dir.is_dir(),
        "findings": {f"{check} {sev}": n for (check, sev), n in sorted(per_check.items())},
        "fail": fails,
        "warn": warns,
        "fingerprint": db_fingerprint(conn),
    }
    if not attachments_dir.is_dir() and any(p.suffix.lower() == ".pst" for p in files):
        summary["warn"] += 1
        findings.append({"check": "attachments", "severity": WARN, "path": "",
                         "detail": "PSTs present but `dsar attachments` has not been run: attachments not ingested"})
    return {"summary": summary, "findings": findings}


def write_ingest_qa(result: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    csv_path = output_dir / f"qa_ingest_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["check", "severity", "path", "detail"], quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(sorted(result["findings"], key=lambda r: ({FAIL: 0, WARN: 1}.get(r["severity"], 2), r["check"])))
    json_path = output_dir / f"qa_ingest_{stamp}.json"
    json_path.write_text(json.dumps({**result["summary"], "findings_csv": csv_path.name}, indent=2), encoding="utf-8")
    return json_path, csv_path


def latest_ingest_pass(output_dir: Path, fingerprint: dict) -> Path | None:
    """Newest ingest QA for exactly this database state, if it passed."""
    for path in sorted(output_dir.glob(INGEST_GLOB), reverse=True):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("fingerprint") == fingerprint:
            return path if doc.get("passed") else None
    return None
