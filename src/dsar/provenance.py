"""
Provenance catalogue: where every ingested item came from.

Joins three sources so each chunk can be traced back to its origin:
  - the files actually present under input/ (hashed now and compared with the
    pre-ingest SHA-256 manifest),
  - Purview's own item report (metadata/Items_*.csv): custodian, workload,
    original location and URL for each exported item,
  - per-case labelling rules from config.yaml (`provenance:`), e.g. marking a
    SharePoint folder as a prior DSAR's case file, so the Personal Data Report
    can attribute items inside it to that source.

Nothing is excluded here: provenance is descriptive only.
"""
import csv
import hashlib
import sqlite3
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable

from dsar import db, pst_attachments
from dsar.config import ClassifyScope, ProvenanceRule


DEFAULT_LABEL = "Primary source (Purview export)"

# Purview's `Target path` names the export ZIP; map each ZIP to the folder it
# was extracted into under input/ (case layout convention: PSTs/, Files/).
_ZIP_PREFIX_DIRS = (("PSTs.", "PSTs"), ("Items.", "Files"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path) -> dict[str, str]:
    """Parse a `shasum -a 256` manifest (comment lines start with #) into {rel_path: sha256}."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        out[rel] = digest
    return out


def label_for(rel_path: str, rules: list[ProvenanceRule]) -> str:
    """First matching rule wins; unmatched files are primary source material."""
    for rule in rules:
        if fnmatchcase(rel_path, rule.match):
            return rule.label
    return DEFAULT_LABEL


def target_to_rel(target_path: str) -> str | None:
    """`Items.1.001.X.zip\\Exchange\\a.json` -> `Files/Exchange/a.json`;
    PST items map to the PST file itself (`PSTs/Exchange.001.pst`)."""
    parts = target_path.split("\\")
    if len(parts) < 2:
        return None
    for prefix, folder in _ZIP_PREFIX_DIRS:
        if parts[0].startswith(prefix):
            if folder == "PSTs":
                return f"PSTs/{parts[1]}"
            return "/".join([folder, *parts[1:]])
    return None


def load_purview_items(metadata_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for csv_path in sorted(metadata_dir.glob("Items_*.csv")):
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "status": r.get("Status") or "",
                    "error_detail": r.get("Error detail") or "",
                    "target_path": r.get("Target path") or "",
                    "message_id": r.get("Internet message ID") or None,
                    "custodian": r.get("Custodian") or r.get("Data source") or "",
                    "workload": r.get("Workload") or "",
                    "compound_path": r.get("Compound path") or r.get("Original path") or "",
                    "url": r.get("SPO document link") or r.get("Preservation original URL") or "",
                    "title": r.get("Subject/Title") or r.get("File name") or "",
                })
    return rows


def build(
    conn: sqlite3.Connection,
    input_dir: Path,
    rules: list[ProvenanceRule],
    manifest: dict[str, str] | None,
) -> dict[str, int]:
    """Rebuild provenance_files + purview_items from what is on disk now."""
    items = load_purview_items(input_dir / "metadata")
    conn.execute("DELETE FROM purview_items")
    conn.executemany(
        """INSERT INTO purview_items (row_no, status, error_detail, target_path, message_id,
                                      custodian, workload, compound_path, url, title)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(i, r["status"], r["error_detail"], r["target_path"], r["message_id"], r["custodian"],
          r["workload"], r["compound_path"], r["url"], r["title"]) for i, r in enumerate(items, start=1)],
    )

    by_rel: dict[str, list[dict]] = {}
    for r in items:
        rel = target_to_rel(r["target_path"])
        if rel:
            by_rel.setdefault(rel, []).append(r)

    # PST attachments extracted by `dsar attachments`: link each stored file to
    # its parent email(s), and through the parent's Message-ID to the Purview
    # custodian/folder, so attachments trace back to a mailbox like emails do.
    attachments = pst_attachments.load_catalogue(input_dir.parent / "output" / pst_attachments.CATALOGUE_NAME)
    conn.execute("DELETE FROM pst_attachments")
    cols = [c for c in pst_attachments.CATALOGUE_FIELDS]
    conn.executemany(
        f"INSERT INTO pst_attachments ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [tuple(a.get(c) or None for c in cols) for a in attachments],
    )
    by_msgid: dict[str, list[dict]] = {}
    for r in items:
        if r["message_id"]:
            by_msgid.setdefault(r["message_id"], []).append(r)
    for a in attachments:
        parents = by_msgid.get(a.get("parent_message_id") or "", [])
        for rel in (a.get("stored_rel_path"), a.get("recovered_text_rel_path")):
            if rel and parents:
                by_rel.setdefault(rel, []).extend(parents)

    counters = {"files": 0, "manifest_match": 0, "manifest_mismatch": 0,
                "not_in_manifest": 0, "purview_matched": 0, "labelled": 0}
    conn.execute("DELETE FROM provenance_files")
    for path in sorted(p for p in input_dir.rglob("*") if p.is_file() and p.name != ".DS_Store"):
        rel = path.relative_to(input_dir).as_posix()
        digest = sha256_file(path)
        expected = (manifest or {}).get(rel)
        if expected is None:
            counters["not_in_manifest"] += 1
        elif expected == digest:
            counters["manifest_match"] += 1
        else:
            counters["manifest_mismatch"] += 1

        matches = by_rel.get(rel, [])
        if matches:
            counters["purview_matched"] += 1
        label = label_for(rel, rules)
        if label != DEFAULT_LABEL:
            counters["labelled"] += 1

        def _joined(key: str) -> str:
            return "; ".join(sorted({m[key] for m in matches if m[key]}))

        # A PST holds many items from many mailboxes; per-email custodian and
        # folder come from purview_items via Message-ID at report time.
        is_pst = path.suffix.lower() == ".pst"
        conn.execute(
            """INSERT INTO provenance_files (source_path, rel_path, sha256, manifest_sha256,
                   provenance_label, purview_custodian, purview_workload, purview_location,
                   purview_url, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(path), rel, digest, expected, label,
             _joined("custodian"), _joined("workload"),
             "(per email - see purview_items)" if is_pst else _joined("compound_path"),
             "" if is_pst else _joined("url"), db._now()),
        )
        counters["files"] += 1
    return counters


def scope_filter(conn: sqlite3.Connection, input_dir: Path, scope: ClassifyScope) -> Callable[[str], bool]:
    """Return a predicate over chunks.source_path implementing config.classify_scope.

    Label-based scope needs the provenance catalogue; refuse to guess without it.
    """
    if scope.is_unrestricted:
        return lambda _source_path: True
    labels = {row[0]: row[1] for row in conn.execute(
        "SELECT source_path, provenance_label FROM provenance_files")}
    if scope.include_labels and not labels:
        raise ValueError("classify_scope uses include_labels but provenance_files is empty - run `dsar provenance` first")
    prefix = str(input_dir).rstrip("/") + "/"

    def in_scope(source_path: str) -> bool:
        rel = source_path[len(prefix):] if source_path.startswith(prefix) else source_path
        if any(fnmatchcase(rel, p) for p in scope.exclude_paths):
            return False
        return (labels.get(source_path) in scope.include_labels
                or any(fnmatchcase(rel, p) for p in scope.include_paths))

    return in_scope


CATALOGUE_SQL = """
SELECT pf.rel_path, pf.provenance_label, pf.sha256, pf.manifest_sha256,
       pf.purview_custodian, pf.purview_workload, pf.purview_location, pf.purview_url,
       (SELECT COUNT(*) FROM chunks c WHERE c.source_path = pf.source_path) AS chunks,
       -- unary + stops SQLite choosing idx_chunks_alias (a near-full scan per file).
       (SELECT COUNT(*) FROM chunks c WHERE c.source_path = pf.source_path AND +c.alias_matched = 1) AS alias_matched_chunks,
       (SELECT group_concat(reason, ' | ') FROM skipped s WHERE s.source_path = pf.source_path) AS skipped_reason
FROM provenance_files pf
ORDER BY pf.provenance_label, pf.rel_path
"""


def export_catalogue(conn: sqlite3.Connection, out_path: Path) -> int:
    rows = conn.execute(CATALOGUE_SQL).fetchall()
    fieldnames = ["rel_path", "provenance_label", "sha256", "manifest_sha256", "manifest_match",
                  "purview_custodian", "purview_workload", "purview_location", "purview_url",
                  "chunks", "alias_matched_chunks", "skipped_reason"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            d = dict(r)
            d["manifest_match"] = ("" if not d["manifest_sha256"]
                                   else "yes" if d["manifest_sha256"] == d["sha256"] else "NO")
            d["skipped_reason"] = d["skipped_reason"] or ""
            w.writerow(d)
    return len(rows)
