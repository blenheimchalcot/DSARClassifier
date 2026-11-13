"""
Review decisions made in the web app, and what they put in the Personal Data Report.

Replaces the review workbook (dsar.review) for deciding on classified items. A
decision is one of DECISIONS. "Extract" carries the passages to extract into the
Personal Data Report and "Release in full with redactions" the passages to redact,
each a character range of the chunk content, tied to the content's SHA-256 so a
range can never land on other text. Decisions are append-only (`decisions`,
`decision_spans`); the newest per chunk is current. The model's score is never
changed.

Decisions imported from workbooks (`review_decisions`) keep their own wording and
count as decided. For the report: a workbook "Disclose" is taken as the whole
passage, flagged so it can be narrowed; a workbook "Disclose with redactions"
recorded what to redact only in notes, so it stays outstanding until the passages
are selected here.
"""
import hashlib
import json
import re
import sqlite3
from collections import Counter

from dsar import db, evidence, report, review

MIN_SCORE = review.MIN_SCORE
EXTRACT = "Extract to Personal Data Report"
RELEASE = "Release in full"
RELEASE_REDACTED = "Release in full with redactions"
WITHHOLD = "Withhold (exemption applies)"
NOT_SUBJECT = "Not the subject's personal data"
DECISIONS = [EXTRACT, RELEASE, RELEASE_REDACTED, WITHHOLD, NOT_SUBJECT]
REASONS = [r for r in review.REASONS if r != "None"]
ASSESSMENTS = review.ASSESSMENTS

# Workbook decisions (dsar.review.DECISIONS) and what they mean for the report.
WB_DISCLOSE, WB_REDACT, WB_WITHHOLD, WB_NOT_SUBJECT = review.DECISIONS

TO_REVIEW, NEEDS_PASSAGES, DONE = "To review", "Needs passages", "Done"


class DecisionError(ValueError):
    pass


def content_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _spans(conn: sqlite3.Connection, decision_id: int) -> list[dict]:
    return [{"kind": r["kind"], "start": r["start"], "end": r["end"], "reason": r["reason"] or ""}
            for r in conn.execute('SELECT kind, start, "end", reason FROM decision_spans WHERE decision_id = ? '
                                  "ORDER BY start", (decision_id,))]


def history(conn: sqlite3.Connection, chunk_id: str) -> list[dict]:
    """Every decision on a chunk, oldest first, from workbooks and from the app."""
    out = []
    for r in conn.execute("SELECT * FROM review_decisions WHERE chunk_id = ? ORDER BY imported_at, rowid", (chunk_id,)):
        out.append({"source": f"workbook {r['workbook']}", "decision": r["decision"], "reason": r["reason"] or "",
                    "notes": r["notes"] or "", "assessment": r["assessment"] or "",
                    "adjusted_score": r["adjusted_score"], "reviewer": r["reviewer"] or "",
                    "at": r["imported_at"], "spans": [], "content_sha256": None, "decision_id": None})
    for r in conn.execute("SELECT * FROM decisions WHERE chunk_id = ? ORDER BY decided_at, decision_id", (chunk_id,)):
        out.append({"source": "app", "decision": r["decision"], "reason": r["reason"] or "", "notes": r["notes"] or "",
                    "assessment": r["assessment"] or "", "adjusted_score": r["adjusted_score"],
                    "reviewer": r["reviewer"], "at": r["decided_at"], "spans": _spans(conn, r["decision_id"]),
                    "content_sha256": r["content_sha256"], "decision_id": r["decision_id"]})
    out.sort(key=lambda d: d["at"])
    return out


def latest(conn: sqlite3.Connection) -> dict[str, dict]:
    """The current decision per chunk: the newest across workbook imports and the app."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT chunk_id, decision, reason, notes, reviewer, imported_at, workbook "
                          "FROM review_decisions ORDER BY imported_at, rowid"):
        out[r["chunk_id"]] = {"source": f"workbook {r['workbook']}", "decision": r["decision"],
                              "reason": r["reason"] or "", "notes": r["notes"] or "",
                              "reviewer": r["reviewer"] or "", "at": r["imported_at"], "decision_id": None}
    for r in conn.execute("SELECT * FROM decisions ORDER BY decided_at, decision_id"):
        prior = out.get(r["chunk_id"])
        if prior and prior["at"] > r["decided_at"]:
            continue
        out[r["chunk_id"]] = {"source": "app", "decision": r["decision"], "reason": r["reason"] or "",
                              "notes": r["notes"] or "", "reviewer": r["reviewer"], "at": r["decided_at"],
                              "decision_id": r["decision_id"], "content_sha256": r["content_sha256"]}
    return out


def status_of(dec: dict | None) -> str:
    if not dec:
        return TO_REVIEW
    if dec["source"] != "app" and dec["decision"] == WB_REDACT:
        return NEEDS_PASSAGES
    return DONE


def _meta(row) -> dict:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _title(row, meta: dict) -> str:
    if row["subject"]:
        return row["subject"]
    return row["source_path"].replace("\\", "/").rsplit("/", 1)[-1]


QUEUE_SQL = """
SELECT c.chunk_id, c.source_type, c.source_path, c.item_ref, c.subject, c.metadata_json, c.content,
       cl.score, cl.description, cl.evidence
FROM chunks c JOIN classifications cl ON cl.chunk_id = c.chunk_id
WHERE c.alias_matched = 1
ORDER BY CASE WHEN cl.score IS NULL THEN -1 ELSE cl.score END DESC,
         COALESCE(c.thread_id, c.chunk_id), c.chunk_id
"""


def queue(conn: sqlite3.Connection, min_score: int = MIN_SCORE, status: str = "", q: str = "") -> dict:
    """Items scored min_score+ (and any item with a decision), with their review status."""
    decided = latest(conn)
    rows, counts = [], Counter()
    q = q.strip().lower()
    for r in conn.execute(QUEUE_SQL):
        if (r["score"] is None or r["score"] < min_score) and r["chunk_id"] not in decided:
            continue
        dec = decided.get(r["chunk_id"])
        st = status_of(dec)
        counts[st] += 1
        if status and st != status:
            continue
        meta = _meta(r)
        if q and q not in " ".join([r["subject"] or "", r["content"] or "", report._from_field(meta, r["source_type"]),
                                    r["source_path"]]).lower():
            continue
        rows.append({"chunk_id": r["chunk_id"], "score": r["score"], "reason": r["description"] or "",
                     "evidence_check": evidence.verify(r["score"], r["evidence"], r["content"], r["subject"]),
                     "source_type": r["source_type"], "date": report._sent_field(meta, r["source_type"]),
                     "from": report._from_field(meta, r["source_type"]), "title": _title(r, meta),
                     "status": st, "decision": dec["decision"] if dec else "", "reviewer": dec["reviewer"] if dec else "",
                     "decision_source": dec["source"] if dec else ""})
    # To review first; within a status the score order from the query is kept.
    order = {TO_REVIEW: 0, NEEDS_PASSAGES: 1, DONE: 2}
    rows.sort(key=lambda x: order[x["status"]])
    return {"rows": rows, "counts": {k: counts.get(k, 0) for k in (TO_REVIEW, NEEDS_PASSAGES, DONE)},
            "total": sum(counts.values())}


def item(conn: sqlite3.Connection, chunk_id: str) -> dict | None:
    r = conn.execute(report.REPORT_SQL.replace("WHERE c.alias_matched = 1", "WHERE c.chunk_id = ?"),
                     (chunk_id,)).fetchone()
    if r is None:
        return None
    meta = _meta(r)
    st = r["source_type"]
    content = r["content"] or ""
    siblings = [{"chunk_id": s["chunk_id"], "score": s["score"], "same_item": s["item_ref"] == r["item_ref"]}
                for s in conn.execute(
                    "SELECT c.chunk_id, c.item_ref, cl.score FROM chunks c LEFT JOIN classifications cl "
                    "ON cl.chunk_id = c.chunk_id WHERE c.chunk_id != ? AND ((c.source_path = ? AND c.item_ref = ?) "
                    "OR (c.thread_id IS NOT NULL AND c.thread_id = ?)) ORDER BY c.chunk_id LIMIT 50",
                    (chunk_id, r["source_path"], r["item_ref"], r["thread_id"]))]
    hist = history(conn, chunk_id)
    return {"chunk_id": chunk_id, "title": _title(r, meta), "source_type": st, "date": report._sent_field(meta, st),
            "from": report._from_field(meta, st), "to": report._to_field(meta, st), "cc": meta.get("cc") or "",
            "subject": r["subject"] or "", "custodian": r["email_custodians"] or r["purview_custodian"] or "",
            "location": r["email_locations"] or r["purview_location"] or "", "source_path": r["source_path"],
            "item_ref": r["item_ref"], "attachment_parents": r["attachment_parents"] or "",
            "score": r["score"], "reason": r["description"] or "", "evidence": r["evidence"] or "",
            "evidence_check": evidence.verify(r["score"], r["evidence"], content, r["subject"]),
            "evidence_spans": review.evidence_spans(content, r["evidence"] or ""),
            "content": content, "content_sha256": content_sha256(content),
            "siblings": siblings, "history": hist, "current": hist[-1] if hist else None,
            "status": status_of(latest_for(conn, chunk_id))}


def latest_for(conn: sqlite3.Connection, chunk_id: str) -> dict | None:
    hist = history(conn, chunk_id)
    return hist[-1] if hist else None


def record(conn: sqlite3.Connection, chunk_id: str, data: dict, reviewer: str) -> dict:
    """Validate and store one decision. Raises DecisionError with every problem found."""
    row = conn.execute("SELECT c.content, cl.score FROM chunks c LEFT JOIN classifications cl "
                       "ON cl.chunk_id = c.chunk_id WHERE c.chunk_id = ?", (chunk_id,)).fetchone()
    if row is None:
        raise DecisionError("item not in this case's database")
    content = row["content"] or ""
    errors = []
    decision = (data.get("decision") or "").strip()
    reason = (data.get("reason") or "").strip()
    notes = (data.get("notes") or "").strip()
    assessment = (data.get("assessment") or "").strip()
    if not reviewer:
        errors.append("sign in first: decisions are recorded under your name")
    if data.get("content_sha256") != content_sha256(content):
        errors.append("the item's text has changed since the page was loaded; reload it")
    if decision not in DECISIONS:
        errors.append("choose a decision")
    if assessment and assessment not in ASSESSMENTS:
        errors.append(f"assessment '{assessment}' is not one of the options")
    adjusted = data.get("adjusted_score")
    if adjusted in ("", None):
        adjusted = None
    else:
        try:
            adjusted = int(adjusted)
            if not 0 <= adjusted <= 10:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("adjusted score must be 0-10")
    spans = []
    for s in data.get("spans") or []:
        try:
            a, b = int(s["start"]), int(s["end"])
        except (KeyError, TypeError, ValueError):
            errors.append("a selected passage has no valid position")
            continue
        kind = s.get("kind")
        if kind not in ("extract", "redact") or not 0 <= a < b <= len(content):
            errors.append("a selected passage is outside the text")
            continue
        if not content[a:b].strip():
            continue
        spans.append({"kind": kind, "start": a, "end": b, "reason": (s.get("reason") or "").strip()})
    extracts = [s for s in spans if s["kind"] == "extract"]
    redacts = [s for s in spans if s["kind"] == "redact"]
    if decision == EXTRACT and not extracts:
        errors.append("select at least one passage to extract (or 'Extract whole passage')")
    if decision == RELEASE_REDACTED:
        if not redacts:
            errors.append("select at least one passage to redact")
        if any(s["reason"] not in REASONS for s in redacts):
            errors.append("every redaction needs a reason")
    if decision == WITHHOLD and reason not in REASONS:
        errors.append("withholding needs the exemption that applies")
    if reason and reason not in REASONS:
        errors.append(f"reason '{reason}' is not one of the options")
    if "Other (explain in notes)" in [reason] + [s["reason"] for s in spans] and not notes:
        errors.append("'Other' needs an explanation in the notes")
    keep = {EXTRACT: extracts, RELEASE_REDACTED: redacts}.get(decision, [])
    for group in (extracts, redacts):
        group.sort(key=lambda s: s["start"])
        if any(x["end"] > y["start"] for x, y in zip(group, group[1:])):
            errors.append("selected passages overlap; remove one")
    if errors:
        raise DecisionError("; ".join(dict.fromkeys(errors)))
    cur = conn.execute(
        "INSERT INTO decisions (chunk_id, content_sha256, model_score, assessment, adjusted_score, decision, reason, "
        "notes, reviewer, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chunk_id, content_sha256(content), row["score"], assessment or None, adjusted, decision,
         reason or None, notes or None, reviewer, db._now()))
    did = cur.lastrowid
    conn.executemany('INSERT INTO decision_spans (decision_id, kind, start, "end", reason) VALUES (?,?,?,?,?)',
                     [(did, s["kind"], s["start"], s["end"], s["reason"] or None) for s in keep])
    conn.commit()
    return {"decision_id": did, "decision": decision, "chunk_id": chunk_id, "content_sha256": content_sha256(content),
            "spans": len(keep), "model_score": row["score"], "adjusted_score": adjusted, "reason": reason}


def _paragraphs(text: str) -> str:
    """Tidy an extract: one line per paragraph, inner whitespace collapsed."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n".join(p for p in paras if p)


def report_inputs(conn: sqlite3.Connection, min_score: int = MIN_SCORE) -> dict:
    """What the review decisions put in the Personal Data Report, and what is still outstanding."""
    decided = latest(conn)
    pieces, outstanding, changed, whole, documents = [], [], [], [], []
    counts = Counter()
    for r in conn.execute(report.REPORT_SQL):
        cid = r["chunk_id"]
        dec = decided.get(cid)
        if dec is None:
            if r["score"] is not None and r["score"] >= min_score:
                outstanding.append({"chunk_id": cid, "why": "not reviewed", "score": r["score"]})
            continue
        counts[dec["decision"]] += 1
        meta = _meta(r)
        st = r["source_type"]
        content = r["content"] or ""
        base = {"origin": "review", "chunk_id": cid, "item_ref": r["item_ref"], "source_path": r["source_path"],
                "date": report._sent_field(meta, st), "source_type": st, "from": report._from_field(meta, st),
                "subject": r["subject"] or "", "decision": dec["decision"], "decision_source": dec["source"],
                "reviewer": dec["reviewer"], "decided_at": dec["at"], "content_sha256": content_sha256(content)}
        if dec["source"] == "app":
            if dec.get("content_sha256") != base["content_sha256"]:
                changed.append({"chunk_id": cid, "why": "text changed since the decision"})
                continue
            if dec["decision"] == EXTRACT:
                for s in _spans(conn, dec["decision_id"]):
                    if s["kind"] == "extract":
                        pieces.append({**base, "text": _paragraphs(content[s["start"]:s["end"]]),
                                       "span": f"{s['start']}-{s['end']}"})
            elif dec["decision"] in (RELEASE, RELEASE_REDACTED):
                documents.append({**base})
        elif dec["decision"] == WB_DISCLOSE:
            pieces.append({**base, "text": _paragraphs(content), "span": "whole passage"})
            whole.append(cid)
        elif dec["decision"] == WB_REDACT:
            outstanding.append({"chunk_id": cid, "why": "workbook 'Disclose with redactions': select the passages",
                                "score": r["score"]})
    return {"pieces": pieces, "outstanding": outstanding, "changed": changed, "whole_from_workbook": whole,
            "documents": documents, "decisions": dict(counts)}
