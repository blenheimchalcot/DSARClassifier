"""
PST ingest with thread-awareness and paragraph-level dedupe.

The old single-file `extract()` is gone: thread grouping and dedupe only make
sense across all PSTs in a case, so the CLI now calls `ingest_emails()` once
with the full list of PST paths.
"""
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.email_thread import (
    hash_paragraph,
    normalise_subject,
    split_paragraphs,
    thread_key_from_headers,
)
from dsar.chunking.prefilter import AliasMatcher
from dsar import db


_HEADER_TO = re.compile(r"^To:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_HEADER_CC = re.compile(r"^Cc:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_HEADER_FROM = re.compile(r"^From:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
# Internet Message-ID is the join key back to Purview's item report, which
# records the custodian mailbox and folder each exported email came from.
_HEADER_MESSAGE_ID = re.compile(r"^Message-ID:\s*(<[^>\r\n]+>)", re.MULTILINE | re.IGNORECASE)


@dataclass
class RawMessage:
    pst_path: str
    identifier: str
    subject: str
    body: str
    headers_raw: str
    sender_name: str
    sent_iso: str | None                # ISO 8601; None if unknown
    to: str | None = None
    cc: str | None = None
    sender_header: str | None = None
    message_id: str | None = None

    def sort_key(self) -> tuple[int, str]:
        # Messages without a date sort after dated ones, but still deterministic.
        return (0, self.sent_iso) if self.sent_iso else (1, self.identifier)


def _archive_messages(pst_path: Path):
    """Every message in a PST, folder by folder.

    Same walk as libratom's `PffArchive.messages()` (which this replaced: libratom
    pins old exact versions of ~15 packages and pulls in spaCy, so it cannot be
    installed alongside current ones). Keeping its order keeps ingest output
    identical for existing cases.
    """
    import logging
    from collections import deque
    import pypff  # lazy import

    log = logging.getLogger(__name__)
    pff = pypff.file()
    pff.open(str(pst_path), "rb")
    try:
        folders = deque([pff.root_folder])
        while folders:
            folder = folders.pop()
            try:
                yield from folder.sub_messages
            except OSError as exc:
                log.debug("unreadable folder in %s: %s", pst_path, exc)
            folders.extendleft(folder.sub_folders)
    finally:
        pff.close()


def iter_messages(pst_paths: list[Path]) -> Iterator[RawMessage]:
    """Walk every .pst via pypff and yield a normalised RawMessage per email.

    Real-world PSTs contain occasional corrupt records that pypff can't parse;
    we skip those individually rather than aborting the whole archive.
    """
    import logging

    log = logging.getLogger(__name__)

    for pst_path in pst_paths:
        for message in _archive_messages(pst_path):
            try:
                yield _to_raw(pst_path, message)
            except Exception as exc:
                log.warning("skipping bad message in %s: %s", pst_path, exc)
                continue


def _to_raw(pst_path: Path, message) -> RawMessage:
    identifier = str(getattr(message, "identifier", "") or "")
    subject = _safe_str(getattr(message, "subject", None))
    headers_raw = _safe_str(getattr(message, "transport_headers", None))
    body = _extract_body(message)
    sender = _safe_str(getattr(message, "sender_name", None))
    sent = getattr(message, "client_submit_time", None)
    sent_iso = sent.isoformat() if sent else None

    return RawMessage(
        pst_path=str(pst_path),
        identifier=identifier,
        subject=subject,
        body=body,
        headers_raw=headers_raw,
        sender_name=sender,
        sent_iso=sent_iso,
        to=_first_match(_HEADER_TO, headers_raw),
        cc=_first_match(_HEADER_CC, headers_raw),
        sender_header=_first_match(_HEADER_FROM, headers_raw),
        message_id=_first_match(_HEADER_MESSAGE_ID, headers_raw),
    )


def ingest_emails(
    messages: Iterator[RawMessage],
    matcher: AliasMatcher,
    conn: sqlite3.Connection,
    max_chars: int,
    overlap_chars: int,
) -> Iterator[Chunk]:
    """
    Main email pipeline:
      1. Buffer all messages, sort chronologically (so earliest sender owns
         shared paragraph hashes).
      2. For each message:
         - strip signature then quoted history
         - split into paragraphs
         - skip paragraphs whose hash already exists in DB (from an earlier chunk)
         - emit a Chunk containing only the net-new paragraphs
         - if the message was entirely quoted/duplicate, emit the chunk with
           skipped_as_duplicate_of pointing at the origin (most recent duplicate
           paragraph's origin) so it's still recorded but never classified.
      3. Thread metadata is written to the `threads` table; paragraph origins
         are written to `paragraph_hashes`.

    Yields Chunks that the caller upserts into `chunks`.
    """
    import logging
    log = logging.getLogger(__name__)

    buffered = sorted(messages, key=lambda m: m.sort_key())

    for msg in buffered:
        try:
            yield from _process_message(msg, matcher, conn, max_chars, overlap_chars)
        except Exception as exc:
            log.warning("skipping message id=%s in %s: %s", msg.identifier, msg.pst_path, exc)
            continue


def _process_message(
    msg: RawMessage,
    matcher: AliasMatcher,
    conn: sqlite3.Connection,
    max_chars: int,
    overlap_chars: int,
) -> Iterator[Chunk]:
    chunk_id = hash_chunk_id(msg.pst_path, f"msg-{msg.identifier}")
    thread_id = thread_key_from_headers(msg.headers_raw, msg.subject, msg.identifier)
    subject_norm = normalise_subject(msg.subject)

    db.upsert_thread(conn, thread_id, subject_norm or None)

    # Empty body → record the [Email empty] marker so the classifier can emit that special return.
    if not msg.body.strip():
        yield Chunk(
            chunk_id=chunk_id,
            source_type="email",
            source_path=msg.pst_path,
            item_ref=f"msg-{msg.identifier}",
            subject=msg.subject or None,
            content="[Email empty]",
            metadata=_email_metadata(msg, note="empty"),
            alias_matched=matcher.matches(msg.subject, msg.to, msg.cc, msg.sender_name, msg.sender_header),
            thread_id=thread_id,
        )
        return

    # No pre-processing of the body: in a DSAR context, both signature stripping
    # and quoted-history stripping silently drop content (e.g. "Kind regards" before
    # a forwarded message would cut the whole forward). Paragraph-level dedupe below
    # handles repetition where it actually matters.
    body = msg.body

    if not body.strip():
        # Everything was quoted/signature — mark as duplicate of its thread's origin.
        origin = _recent_thread_origin(conn, thread_id, exclude=chunk_id) or thread_id
        yield Chunk(
            chunk_id=chunk_id,
            source_type="email",
            source_path=msg.pst_path,
            item_ref=f"msg-{msg.identifier}",
            subject=msg.subject or None,
            content="[All content was quoted history from earlier in the thread.]",
            metadata=_email_metadata(msg, note="all-quoted"),
            alias_matched=False,  # nothing to classify; filter drops it anyway
            thread_id=thread_id,
            skipped_as_duplicate_of=origin,
        )
        return

    # Paragraph-level dedupe.
    paragraphs = split_paragraphs(body)
    kept_paras: list[str] = []
    dup_origins: list[str] = []
    new_hashes: list[str] = []

    for para in paragraphs:
        h = hash_paragraph(para)
        if not h:
            # Too short to hash meaningfully — keep it so we don't drop real content.
            kept_paras.append(para)
            continue
        origin = db.paragraph_hash_origin(conn, h)
        if origin is None:
            kept_paras.append(para)
            new_hashes.append(h)
        else:
            dup_origins.append(origin)

    if not kept_paras:
        # Every paragraph was already classified under an earlier chunk.
        origin = dup_origins[-1] if dup_origins else thread_id
        yield Chunk(
            chunk_id=chunk_id,
            source_type="email",
            source_path=msg.pst_path,
            item_ref=f"msg-{msg.identifier}",
            subject=msg.subject or None,
            content="[All paragraphs already classified in earlier chunk(s).]",
            metadata=_email_metadata(msg, note="all-duplicate", duplicate_origins=dup_origins),
            alias_matched=False,
            thread_id=thread_id,
            skipped_as_duplicate_of=origin,
        )
        return

    # Record hashes *before* yielding so subsequent messages in this run see them.
    for h in new_hashes:
        db.record_paragraph_hash(conn, h, chunk_id)

    kept_body = "\n\n".join(kept_paras)
    parts = split_long(kept_body, max_chars=max_chars, overlap_chars=overlap_chars)

    for i, part in enumerate(parts):
        part_ref = f"msg-{msg.identifier}" if len(parts) == 1 else f"msg-{msg.identifier}#part-{i + 1}"
        part_chunk_id = chunk_id if len(parts) == 1 else hash_chunk_id(msg.pst_path, part_ref)
        content = part if not msg.subject else f"Subject: {msg.subject}\n\n{part}"

        yield Chunk(
            chunk_id=part_chunk_id,
            source_type="email",
            source_path=msg.pst_path,
            item_ref=part_ref,
            subject=msg.subject or None,
            content=content,
            metadata=_email_metadata(
                msg,
                note=None,
                duplicate_origins=dup_origins or None,
                paragraphs_kept=len(kept_paras),
                paragraphs_total=len(paragraphs),
                part=i + 1 if len(parts) > 1 else None,
            ),
            alias_matched=matcher.matches(
                part, msg.subject, msg.to, msg.cc, msg.sender_name, msg.sender_header
            ),
            thread_id=thread_id,
        )


def _email_metadata(msg: RawMessage, **extras) -> dict:
    base = {
        "sender": msg.sender_name or msg.sender_header,
        "to": msg.to,
        "cc": msg.cc,
        "sent": msg.sent_iso,
        "message_id": msg.message_id,
    }
    for k, v in extras.items():
        if v is not None:
            base[k] = v
    return base


def _recent_thread_origin(conn: sqlite3.Connection, thread_id: str, *, exclude: str) -> str | None:
    row = conn.execute(
        """
        SELECT chunk_id FROM chunks
        WHERE thread_id = ? AND chunk_id != ? AND skipped_as_duplicate_of IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (thread_id, exclude),
    ).fetchone()
    return row[0] if row else None


def _safe_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        s = value.decode("utf-8", errors="replace")
    else:
        s = str(value)
    # PST strings occasionally contain stray UTF-16 surrogate halves that pypff
    # passes through verbatim — these blow up SQLite / json.dumps. Round-tripping
    # through encode(replace) substitutes them with U+FFFD.
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _extract_body(message) -> str:
    # Modern Outlook stores bodies as HTML or RTF; plain_text_body is empty for ~90%
    # of messages in real corpora. Try plain → HTML → RTF and stop at the first non-empty.
    # pypff property access raises on corrupt records, so each read is independently guarded.
    plain = _read_body_attr(message, "plain_text_body")
    if plain.strip():
        return plain

    html = _read_body_attr(message, "html_body")
    if html.strip():
        from bs4 import BeautifulSoup  # lazy import
        try:
            text = BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()
            return _safe_str(text)  # BS can reintroduce surrogates via HTML-entity decoding.
        except Exception:
            return ""

    rtf = _read_body_attr(message, "rtf_body")
    if rtf.strip():
        from striprtf.striprtf import rtf_to_text  # lazy import
        try:
            return _safe_str(rtf_to_text(rtf, errors="ignore").strip())
        except Exception:
            return ""

    return ""


def _read_body_attr(message, name: str) -> str:
    try:
        return _safe_str(getattr(message, name, None))
    except Exception:
        return ""


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None
