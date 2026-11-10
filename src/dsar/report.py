import csv
import json
import sqlite3
from pathlib import Path

from dsar import evidence


# Pull the per-chunk row + its thread's chunk count via a window-style join.
# Sorted score DESC, then by thread + sent date so chunks in the same thread
# are adjacent within their score band — reviewers can read a conversation
# top-to-bottom without re-sorting.
REPORT_SQL = """
WITH thread_counts AS (
    SELECT thread_id, COUNT(*) AS thread_chunk_count
    FROM chunks WHERE thread_id IS NOT NULL GROUP BY thread_id
)
SELECT
    c.chunk_id,
    c.source_type,
    c.source_path,
    c.item_ref,
    c.subject,
    c.alias_matched,
    c.thread_id,
    COALESCE(tc.thread_chunk_count, 1) AS thread_chunk_count,
    c.skipped_as_duplicate_of,
    c.metadata_json,
    c.content,
    cl.score,
    cl.description,
    cl.evidence,
    cl.special_return,
    cl.model,
    cl.raw_response,
    pf.provenance_label,
    pf.purview_custodian,
    pf.purview_location,
    pf.purview_url,
    -- Emails: a PST mixes mailboxes, so resolve custodian/folder per message.
    (SELECT group_concat(DISTINCT pi.custodian) FROM purview_items pi
      WHERE pi.message_id = json_extract(c.metadata_json, '$.message_id')) AS email_custodians,
    (SELECT group_concat(DISTINCT pi.compound_path) FROM purview_items pi
      WHERE pi.message_id = json_extract(c.metadata_json, '$.message_id')) AS email_locations,
    -- PST attachments: every parent email the stored file was attached to.
    (SELECT COUNT(*) FROM pst_attachments pa
      WHERE pa.stored_rel_path = pf.rel_path OR pa.recovered_text_rel_path = pf.rel_path) AS attachment_instances,
    (SELECT group_concat(pa.original_name || ' [attached to: ' || COALESCE(pa.parent_subject, '') || ' | '
                         || COALESCE(pa.parent_sender, '') || ' | ' || COALESCE(pa.parent_sent, '') || ' | '
                         || COALESCE(pa.parent_message_id, '') || ']', ' ;; ')
       FROM pst_attachments pa
      WHERE pa.stored_rel_path = pf.rel_path OR pa.recovered_text_rel_path = pf.rel_path) AS attachment_parents
FROM chunks c
LEFT JOIN classifications cl ON cl.chunk_id = c.chunk_id
LEFT JOIN thread_counts tc ON tc.thread_id = c.thread_id
LEFT JOIN provenance_files pf ON pf.source_path = c.source_path
WHERE c.alias_matched = 1
ORDER BY
    CASE WHEN cl.score IS NULL THEN -1 ELSE cl.score END DESC,
    COALESCE(c.thread_id, c.chunk_id),
    COALESCE(json_extract(c.metadata_json, '$.sent'),
             json_extract(c.metadata_json, '$.first_sent'),
             json_extract(c.metadata_json, '$.createdDateTime'),
             '')
"""


def _from_field(meta: dict, source_type: str) -> str:
    """Map source-type-specific 'sender' fields to a single From column."""
    if source_type == "email":
        return meta.get("sender") or ""
    if source_type == "chat_message":
        emails = meta.get("sender_emails") or []
        names = meta.get("participants") or []
        return "; ".join(emails) if emails else "; ".join(names)
    if source_type == "substrate_record":
        return meta.get("createdBy") or ""
    if source_type == "meeting_transcript":
        return meta.get("OrganizerId") or ""
    return meta.get("sender") or meta.get("createdBy") or ""


def _to_field(meta: dict, source_type: str) -> str:
    if source_type == "email":
        return meta.get("to") or ""
    if source_type == "chat_message":
        # In a chat the participants are both From and To collectively.
        return "; ".join(meta.get("participants") or [])
    if source_type == "substrate_record":
        return meta.get("completedBy") or ""
    return meta.get("to") or ""


def _sent_field(meta: dict, source_type: str) -> str:
    if source_type == "email":
        return meta.get("sent") or ""
    if source_type == "chat_message":
        return meta.get("first_sent") or ""
    if source_type == "substrate_record":
        return meta.get("createdDateTime") or ""
    return meta.get("sent") or meta.get("first_sent") or meta.get("createdDateTime") or ""


def export_csv(conn: sqlite3.Connection, out_path: str | Path) -> int:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(REPORT_SQL).fetchall()

    fieldnames = [
        "score", "description", "evidence", "evidence_in_source",
        "source_type",
        "sent_at", "from", "to", "cc",
        "subject",
        "thread_id", "thread_chunk_count",
        "is_duplicate_of",
        "content",
        "raw_response",
        "chunk_id", "item_ref", "source_path", "model",
        "provenance", "purview_custodian", "purview_location", "purview_url",
        "attachment_instances", "attachment_parent_emails",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in rows:
            try:
                meta = json.loads(r["metadata_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            source_type = r["source_type"]
            score = r["score"] if r["score"] is not None else (r["special_return"] or "")
            writer.writerow({
                "score": score,
                "description": r["description"] or "",
                "evidence": r["evidence"] or "",
                "evidence_in_source": evidence.verify(r["score"], r["evidence"], r["content"], r["subject"]),
                "source_type": source_type,
                "sent_at": _sent_field(meta, source_type),
                "from": _from_field(meta, source_type),
                "to": _to_field(meta, source_type),
                "cc": meta.get("cc") or "",
                "subject": r["subject"] or "",
                "thread_id": r["thread_id"] or "",
                "thread_chunk_count": r["thread_chunk_count"] or 1,
                "is_duplicate_of": r["skipped_as_duplicate_of"] or "",
                "content": r["content"] or "",
                "raw_response": (r["raw_response"] or "").strip(),
                "chunk_id": r["chunk_id"],
                "item_ref": r["item_ref"],
                "source_path": r["source_path"],
                "model": r["model"] or "",
                "provenance": r["provenance_label"] or "",
                "purview_custodian": r["email_custodians"] or r["purview_custodian"] or "",
                "purview_location": r["email_locations"] or r["purview_location"] or "",
                "purview_url": r["purview_url"] or "",
                "attachment_instances": r["attachment_instances"] or "",
                "attachment_parent_emails": r["attachment_parents"] or "",
            })
    return len(rows)
