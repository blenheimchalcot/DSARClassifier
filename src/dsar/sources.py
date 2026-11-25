"""
Where an item came from (its source type) and when it was ingested (its collection).

Source type: declared per file at `dsar provenance` time by a config rule's
`source:` (stored in provenance_files.source_type). Files with no declared
source get one worked out from what Purview recorded (workload, URL) and the
path under input/; anything that can't be worked out is "Other".

Collection: each `dsar ingest` run is one collection, a batch of material
brought into the case. Runs are found from chunks.created_at: a gap of more
than COLLECTION_GAP between consecutive chunks starts a new collection.
"""
import sqlite3
from datetime import datetime, timedelta

# The source types a case can draw on. A config rule's `source:` must be one of these.
SOURCE_TYPES = (
    "Purview Email",
    "Purview Teams",
    "Purview SharePoint",
    "Purview OneDrive",
    "Ad hoc files",
    "Database extract",
    "Live platform connection",
    "Purview metadata",
    "Other",
)
COLLECTION_GAP = timedelta(minutes=30)


def check_source(value: str) -> str:
    if value and value not in SOURCE_TYPES:
        raise ValueError(f"unknown source {value!r}: use one of {', '.join(SOURCE_TYPES)}")
    return value


def infer(rel_path: str, label: str, workload: str, url: str) -> str:
    """A source type for a file whose config rule declares none."""
    rel, w, u = rel_path.replace("\\", "/"), (workload or "").lower(), (url or "").lower()
    if rel.startswith("metadata/") or label.startswith("Purview export metadata"):
        return "Purview metadata"
    if "teams" in w or "/teamsmessagesdata/" in rel.lower():
        return "Purview Teams"
    if "onedrive" in w or "-my.sharepoint.com" in u or rel.startswith("Files/OneDrive/"):
        return "Purview OneDrive"
    if "sharepoint" in w or rel.startswith("Files/SharePoint/"):
        return "Purview SharePoint"
    if "exchange" in w or rel.startswith(("PSTs/", "PST_attachments/")) or rel.lower().endswith(".pst"):
        return "Purview Email"
    if label.startswith("Supplementary"):
        return "Ad hoc files"
    return "Other"


def for_chunks(conn: sqlite3.Connection, chunk_ids: list[str]) -> dict[str, dict]:
    """{chunk_id: {"source": source type, "collection": collection label}} for the chunks given."""
    has_col = any(r[1] == "source_type" for r in conn.execute("PRAGMA table_info(provenance_files)"))
    runs = collections(conn)
    out = {}
    for cid in chunk_ids:
        row = conn.execute(
            f"""SELECT c.created_at, pf.rel_path, pf.provenance_label, pf.purview_workload, pf.purview_url,
                       {"pf.source_type" if has_col else "''"}
                FROM chunks c LEFT JOIN provenance_files pf ON pf.source_path = c.source_path
                WHERE c.chunk_id = ?""", (cid,)).fetchone()
        if not row:
            continue
        created, rel, label, workload, url, declared = row
        source = declared or (infer(rel, label or "", workload or "", url or "") if rel is not None else "Other")
        out[cid] = {"source": source, "collection": collection_of(created, runs)}
    return out


def collections(conn: sqlite3.Connection) -> list[dict]:
    """The case's ingest runs, oldest first: [{"start", "end", "chunks", "label"}].
    The label is the run's start time and what most of it was (its commonest provenance label)."""
    runs: list[dict] = []
    prev = None
    for ts, prov, n in conn.execute(
            """SELECT c.created_at, COALESCE(pf.provenance_label, ''), COUNT(*) FROM chunks c
               LEFT JOIN provenance_files pf ON pf.source_path = c.source_path
               GROUP BY c.created_at, pf.provenance_label ORDER BY c.created_at"""):
        t = _parse(ts)
        if t is None:
            continue
        if prev is None or t - prev > COLLECTION_GAP:
            runs.append({"start": ts, "end": ts, "chunks": 0, "labels": {}})
        r = runs[-1]
        r["end"] = ts
        r["chunks"] += n
        r["labels"][prov] = r["labels"].get(prov, 0) + n
        prev = t
    for r in runs:
        what = _short(max(r.pop("labels").items(), key=lambda kv: kv[1])[0])
        r["label"] = _parse(r["start"]).strftime("%d %b %H:%M") + (f" · {what}" if what else "")
    return runs


def _short(label: str) -> str:
    """'Primary source (Purview export)' -> 'Purview export'; 'Supplementary source: provided by X (date)' -> 'provided by X'."""
    if ":" in label:
        return label.split(":", 1)[1].split(" (")[0].strip()
    if "(" in label and label.endswith(")"):
        return label[label.index("(") + 1:-1]
    return label


def collection_of(created_at: str, runs: list[dict]) -> str:
    for r in runs:
        if r["start"] <= created_at <= r["end"]:
            return r["label"]
    return "Unknown"


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
