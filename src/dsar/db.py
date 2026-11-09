import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from dsar.chunking import Chunk


SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id                 TEXT PRIMARY KEY,
    source_type              TEXT NOT NULL,
    source_path              TEXT NOT NULL,
    item_ref                 TEXT NOT NULL,
    subject                  TEXT,
    content                  TEXT NOT NULL,
    metadata_json            TEXT,
    alias_matched            INTEGER NOT NULL,
    thread_id                TEXT,
    skipped_as_duplicate_of  TEXT,
    created_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_alias ON chunks(alias_matched);
CREATE INDEX IF NOT EXISTS idx_chunks_source_type ON chunks(source_type);
CREATE INDEX IF NOT EXISTS idx_chunks_thread ON chunks(thread_id);
CREATE INDEX IF NOT EXISTS idx_chunks_skipped ON chunks(skipped_as_duplicate_of);

CREATE TABLE IF NOT EXISTS threads (
    thread_id           TEXT PRIMARY KEY,
    subject_normalised  TEXT,
    message_count       INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paragraph_hashes (
    hash            TEXT PRIMARY KEY,
    first_chunk_id  TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classifications (
    chunk_id           TEXT PRIMARY KEY,
    score              INTEGER,
    description        TEXT,
    evidence           TEXT,
    special_return     TEXT,
    raw_response       TEXT NOT NULL,
    model              TEXT NOT NULL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    classified_at      TEXT NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_classifications_score ON classifications(score);

CREATE TABLE IF NOT EXISTS skipped (
    source_path  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    skipped_at   TEXT NOT NULL,
    PRIMARY KEY (source_path, reason)
);

-- Provenance: one row per file under input/, written by `dsar provenance`.
-- sha256 is computed at catalogue time and compared with the pre-ingest manifest.
CREATE TABLE IF NOT EXISTS provenance_files (
    source_path        TEXT PRIMARY KEY,
    rel_path           TEXT NOT NULL,
    sha256             TEXT NOT NULL,
    manifest_sha256    TEXT,
    provenance_label   TEXT NOT NULL,
    purview_custodian  TEXT,
    purview_workload   TEXT,
    purview_location   TEXT,
    purview_url        TEXT,
    recorded_at        TEXT NOT NULL
);

-- Purview's own item report (metadata/Items_*.csv), trimmed to the fields
-- needed to trace an item back to its custodian and original location.
CREATE TABLE IF NOT EXISTS purview_items (
    row_no         INTEGER PRIMARY KEY,
    status         TEXT,
    error_detail   TEXT,
    target_path    TEXT,
    message_id     TEXT,
    custodian      TEXT,
    workload       TEXT,
    compound_path  TEXT,
    url            TEXT,
    title          TEXT
);

CREATE INDEX IF NOT EXISTS idx_purview_items_msgid ON purview_items(message_id);

-- Per-file lookups (provenance catalogue, report joins) were full scans; with
-- thousands of extracted PST attachments that turned minutes into hours.
CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON chunks(source_path);

-- One row per attachment instance inside a PST (from `dsar attachments`'
-- catalogue), linking the stored file back to its parent email.
CREATE TABLE IF NOT EXISTS pst_attachments (
    pst_rel                  TEXT NOT NULL,
    parent_identifier        TEXT NOT NULL,
    attach_index             INTEGER NOT NULL,
    parent_message_id        TEXT,
    parent_subject           TEXT,
    parent_sender            TEXT,
    parent_sent              TEXT,
    attach_method            INTEGER,
    original_name            TEXT,
    mime_type                TEXT,
    content_id               TEXT,
    embedded_class           TEXT,
    size_bytes               INTEGER,
    sha256                   TEXT,
    stored_rel_path          TEXT,
    recovered_text_rel_path  TEXT,
    first_instance           TEXT,
    note                     TEXT,
    PRIMARY KEY (pst_rel, parent_identifier, attach_index)
);

CREATE INDEX IF NOT EXISTS idx_pst_attachments_stored ON pst_attachments(stored_rel_path);
CREATE INDEX IF NOT EXISTS idx_pst_attachments_text ON pst_attachments(recovered_text_rel_path);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def upsert_chunk(conn: sqlite3.Connection, chunk: Chunk) -> None:
    conn.execute(
        """
        INSERT INTO chunks (chunk_id, source_type, source_path, item_ref, subject,
                            content, metadata_json, alias_matched, thread_id,
                            skipped_as_duplicate_of, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            source_type=excluded.source_type,
            source_path=excluded.source_path,
            item_ref=excluded.item_ref,
            subject=excluded.subject,
            content=excluded.content,
            metadata_json=excluded.metadata_json,
            alias_matched=excluded.alias_matched,
            thread_id=excluded.thread_id,
            skipped_as_duplicate_of=excluded.skipped_as_duplicate_of
        """,
        (
            chunk.chunk_id,
            chunk.source_type,
            chunk.source_path,
            chunk.item_ref,
            chunk.subject,
            chunk.content,
            json.dumps(chunk.metadata, default=str),
            1 if chunk.alias_matched else 0,
            chunk.thread_id,
            chunk.skipped_as_duplicate_of,
            _now(),
        ),
    )


def upsert_thread(conn: sqlite3.Connection, thread_id: str, subject_normalised: str | None) -> None:
    conn.execute(
        """
        INSERT INTO threads (thread_id, subject_normalised, message_count, first_seen)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            message_count = message_count + 1
        """,
        (thread_id, subject_normalised, _now()),
    )


def record_skipped(conn: sqlite3.Connection, source_path: str, reason: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO skipped (source_path, reason, skipped_at)
        VALUES (?, ?, ?)
        """,
        (source_path, reason, _now()),
    )


def paragraph_hash_origin(conn: sqlite3.Connection, hash_: str) -> str | None:
    """Return the chunk_id that first carried this paragraph hash, or None if unseen."""
    row = conn.execute(
        "SELECT first_chunk_id FROM paragraph_hashes WHERE hash = ?", (hash_,)
    ).fetchone()
    return row[0] if row else None


def record_paragraph_hash(conn: sqlite3.Connection, hash_: str, chunk_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO paragraph_hashes (hash, first_chunk_id, first_seen_at)
        VALUES (?, ?, ?)
        """,
        (hash_, chunk_id, _now()),
    )


def insert_classification(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    score: int | None,
    description: str | None,
    evidence: str | None,
    special_return: str | None,
    raw_response: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO classifications
            (chunk_id, score, description, evidence, special_return, raw_response,
             model, input_tokens, output_tokens, cache_read_tokens, classified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk_id,
            score,
            description,
            evidence,
            special_return,
            raw_response,
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            _now(),
        ),
    )


def unclassified_chunk_ids(
    conn: sqlite3.Connection,
    limit: int | None = None,
    in_scope: Callable[[str], bool] | None = None,
) -> list[str]:
    """
    Return chunks that are alias-matched, not flagged as duplicates, have no
    classification row yet, and (if given) whose source_path passes `in_scope`.
    """
    sql = """
        SELECT c.chunk_id, c.source_path FROM chunks c
        LEFT JOIN classifications cl ON cl.chunk_id = c.chunk_id
        WHERE c.alias_matched = 1
          AND c.skipped_as_duplicate_of IS NULL
          AND cl.chunk_id IS NULL
        ORDER BY c.created_at
    """
    ids = [row[0] for row in conn.execute(sql) if in_scope is None or in_scope(row[1])]
    return ids if limit is None else ids[:limit]


def get_chunk(conn: sqlite3.Connection, chunk_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
