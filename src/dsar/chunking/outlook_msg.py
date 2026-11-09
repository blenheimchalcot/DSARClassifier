"""
Outlook ``.msg`` (CFB / OLE-format saved email) chunker.

These appear on SharePoint when someone right-clicks an email in Outlook and
saves it to a folder. The ``extract-msg`` library exposes subject / sender /
recipients / body / html_body / rtf_body. We mirror the PST chunker's body
fallback (plain → html → rtf) and the same header-and-body content shape so the
classifier sees a familiar message.
"""
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


def _strip_html(text: str) -> str:
    from bs4 import BeautifulSoup  # lazy import
    return BeautifulSoup(text, "html.parser").get_text("\n", strip=True)


def _strip_rtf(text: str) -> str:
    from striprtf.striprtf import rtf_to_text  # lazy import
    try:
        return rtf_to_text(text)
    except Exception:
        return ""


def _resolve_body(msg) -> str:
    """Try plain → html → rtf, matching the PST chunker policy in methodology §5.1."""
    body = (getattr(msg, "body", None) or "").strip()
    if body:
        return body
    html = getattr(msg, "htmlBody", None) or getattr(msg, "html_body", None)
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8", errors="replace")
        except Exception:
            html = ""
    if html:
        stripped = _strip_html(html).strip()
        if stripped:
            return stripped
    rtf = getattr(msg, "rtfBody", None) or getattr(msg, "rtf_body", None)
    if isinstance(rtf, bytes):
        try:
            rtf = rtf.decode("latin-1", errors="replace")
        except Exception:
            rtf = ""
    if rtf:
        stripped = _strip_rtf(rtf).strip()
        if stripped:
            return stripped
    return ""


def _format_header(msg) -> str:
    sender = getattr(msg, "sender", "") or ""
    to = getattr(msg, "to", "") or ""
    cc = getattr(msg, "cc", "") or ""
    date = getattr(msg, "date", "") or ""
    subject = getattr(msg, "subject", "") or ""
    return "\n".join([
        f"From: {sender}",
        f"To: {to}",
        f"Cc: {cc}",
        f"Date: {date}",
        f"Subject: {subject}",
    ])


def extract(path: Path, matcher: AliasMatcher, max_chars: int, overlap_chars: int) -> Iterator[Chunk]:
    import extract_msg  # lazy import

    try:
        msg = extract_msg.openMsg(str(path))
    except Exception as exc:
        # Caller (cli.py) records the skipped entry on exception
        raise

    try:
        header = _format_header(msg)
        body = _resolve_body(msg)
        full_text = f"{header}\n\n{body}".strip()
        if not full_text:
            return

        parts = split_long(full_text, max_chars=max_chars, overlap_chars=overlap_chars)
        for i, part in enumerate(parts):
            item_ref = "full" if len(parts) == 1 else f"part-{i+1}"
            # Match against full text plus sender/recipients explicitly: in some .msg
            # files the body says "FYI" and the relevant alias is only in headers.
            alias_match_text = "\n".join([
                part,
                getattr(msg, "sender", "") or "",
                getattr(msg, "to", "") or "",
                getattr(msg, "cc", "") or "",
                getattr(msg, "subject", "") or "",
            ])
            yield Chunk(
                chunk_id=hash_chunk_id(str(path), item_ref),
                source_type="email",
                source_path=str(path),
                item_ref=item_ref,
                subject=getattr(msg, "subject", None) or path.stem,
                content=part,
                metadata={
                    "format": "msg",
                    "sender": getattr(msg, "sender", "") or "",
                    "date": str(getattr(msg, "date", "") or ""),
                    "size_bytes": path.stat().st_size,
                },
                alias_matched=matcher.matches(alias_match_text),
            )
    finally:
        try:
            msg.close()
        except Exception:
            pass
