import hashlib
import re


_SIGNATURE_DELIMITERS = [
    re.compile(r"^-- ?$", re.MULTILINE),
    re.compile(r"^(Regards|Thanks|Best|Cheers|Sincerely|Kind regards)[,.]?\s*$",
               re.MULTILINE | re.IGNORECASE),
    re.compile(r"^Sent from my (iPhone|iPad|Android|mobile)", re.MULTILINE | re.IGNORECASE),
]


def hash_chunk_id(source_path: str, item_ref: str) -> str:
    h = hashlib.sha1(f"{source_path}::{item_ref}".encode("utf-8")).hexdigest()
    return h[:16]


def strip_signature(body: str, preserve_if_matches: re.Pattern[str] | None = None) -> str:
    """
    Trim an email signature from the end of body. If `preserve_if_matches` is given
    and the stripped signature contains an alias, return the original body instead
    (the signature might carry the subject's PI and we can't drop it silently).
    """
    earliest = len(body)
    for rx in _SIGNATURE_DELIMITERS:
        m = rx.search(body)
        if m and m.start() < earliest:
            earliest = m.start()
    if earliest == len(body):
        return body
    stripped = body[:earliest].rstrip()
    tail = body[earliest:]
    if preserve_if_matches is not None and preserve_if_matches.search(tail):
        return body
    return stripped


def split_long(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """
    Split `text` into chunks <= max_chars, preferring paragraph boundaries, with
    `overlap_chars` character overlap between adjacent chunks. Returns [text] if
    it already fits.
    """
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    parts: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = para if not current else f"{current}\n\n{para}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = (current[-overlap_chars:] + "\n\n" + para) if overlap_chars else para
            if len(current) <= max_chars:
                continue
            # The carried-over paragraph is itself oversized. Previously it was
            # kept whole, producing chunks far beyond the model's context window
            # (prompt truncated -> unclassifiable). Fall through and hard-split.
            para, current = current, ""
        if not current:
            # Single paragraph exceeds max_chars — hard-split it.
            start = 0
            while start < len(para):
                end = min(len(para), start + max_chars)
                parts.append(para[start:end])
                start = end - overlap_chars if end < len(para) else end
            current = ""
    if current:
        parts.append(current)
    return parts
