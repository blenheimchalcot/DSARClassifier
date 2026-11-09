"""
Email thread awareness: subject normalisation, quote-history stripping, and
paragraph-level hashing so we can dedupe text that repeats across replies and
forwards in the same DSAR corpus.
"""
import hashlib
import re

# Prefixes to strip from the front of a subject line when building a thread key.
# Ordered so longer / language-specific forms match before plain RE:.
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(re|fw|fwd|aw|tr|res|rv|sv|wg|vs)(\s*\[\s*\d+\s*\])?\s*:\s*",
    re.IGNORECASE,
)

# Quote-history boundary markers. We cut the body at the FIRST match of any.
_QUOTE_MARKERS: list[re.Pattern[str]] = [
    # Outlook forward header
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    # Outlook horizontal rule separator
    re.compile(r"^_{5,}\s*$", re.MULTILINE),
    # "On <date>, <name> wrote:" — Gmail / modern mail clients
    re.compile(
        r"^On\s.{3,200}\bwrote:\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Contiguous Outlook header block: From: / Sent: / To: / Subject:
    re.compile(
        r"^From:\s.+\r?\n(?:Sent|Date):\s.+\r?\n(?:To:\s.+\r?\n)?(?:Cc:\s.+\r?\n)?Subject:\s.+$",
        re.MULTILINE,
    ),
]

# A line that starts a plain-text quoted block: 3+ consecutive lines starting with ">".
_PLAIN_QUOTE_BLOCK = re.compile(r"(?m:^>.*\n){3,}")


def normalise_subject(subject: str | None) -> str:
    """
    Strip RE:/FW:/FWD: etc. prefixes (repeatedly) and collapse whitespace so that
    all messages in a thread map to the same subject key.
    """
    if not subject:
        return ""
    s = subject
    # Strip repeated prefixes like "Re: Fwd: Re: ..."
    for _ in range(8):
        new = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip()


def strip_quoted_history(body: str) -> str:
    """
    Remove quoted history from an email body so only the text the sender actually
    typed for this message survives. Cuts at the earliest recognised marker.
    Returns the stripped body (may be empty if the whole body was quoted).
    """
    if not body:
        return body
    cut = len(body)
    for rx in _QUOTE_MARKERS:
        m = rx.search(body)
        if m and m.start() < cut:
            cut = m.start()
    m = _PLAIN_QUOTE_BLOCK.search(body)
    if m and m.start() < cut:
        cut = m.start()
    return body[:cut].rstrip()


def _normalise_paragraph(para: str) -> str:
    """
    Canonicalise a paragraph for hashing: lowercase, strip punctuation/quoting
    markers, collapse whitespace. Minor edits (capitalisation, extra spaces,
    trailing punctuation) shouldn't defeat dedupe.
    """
    p = para.lower()
    # Drop leading reply quote markers so "> hi team" dedupes with "hi team".
    p = re.sub(r"^>+\s*", "", p, flags=re.MULTILINE)
    # Keep letters/digits/whitespace only.
    p = re.sub(r"[^\w\s]", " ", p)
    p = re.sub(r"\s+", " ", p).strip()
    return p


MIN_PARAGRAPH_CHARS = 40  # shorter paragraphs are too generic to dedupe on


def hash_paragraph(para: str) -> str:
    """Return a stable hash of a paragraph's canonical form. Short/empty → ''."""
    normalised = _normalise_paragraph(para)
    if len(normalised) < MIN_PARAGRAPH_CHARS:
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def split_paragraphs(text: str) -> list[str]:
    """Split body on blank lines; drop empties; preserve order."""
    if not text:
        return []
    parts = re.split(r"\n\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()]


def thread_key_from_headers(headers_raw: str, subject: str | None, fallback_id: str) -> str:
    """
    Derive a stable thread id:
      1. If References header is present, use the first message-id in it (thread root).
      2. Else if In-Reply-To is present, use that.
      3. Else if this message's own Message-ID is present, use it — replies to this
         message will carry the same id in their References/In-Reply-To.
      4. Else if subject normalises to non-empty, hash the normalised subject.
      5. Else fall back to the given message identifier (singleton thread).
    """
    if headers_raw:
        refs = re.search(r"^References:\s*(.+)$", headers_raw, re.MULTILINE | re.IGNORECASE)
        if refs:
            first = re.search(r"<[^>]+>", refs.group(1))
            if first:
                return _hash_id(first.group(0))
        reply = re.search(r"^In-Reply-To:\s*(<[^>]+>)", headers_raw, re.MULTILINE | re.IGNORECASE)
        if reply:
            return _hash_id(reply.group(1))
        own = re.search(r"^Message-ID:\s*(<[^>]+>)", headers_raw, re.MULTILINE | re.IGNORECASE)
        if own:
            return _hash_id(own.group(1))
    norm = normalise_subject(subject)
    if norm:
        return "subj-" + _hash_id(norm.lower())
    return "msg-" + _hash_id(fallback_id)


def _hash_id(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
