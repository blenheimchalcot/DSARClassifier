"""Check that the evidence a model quotes for a score is actually in the source text.

The model can return evidence it did not take from the chunk (one case observed:
the score-10 example from the system prompt, quoted word for word). Nothing the
model quotes is trusted until it is found in the chunk.
"""
import re
import unicodedata

EVIDENCE_REQUIRED_FROM = 7   # per the prompt: evidence only when score >= 7
PARTIAL_THRESHOLD = 0.8      # share of the quote's 4-word runs found in the source

YES, PARTIAL, NO, MISSING = "yes", "partial", "no", "missing"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\\n", " ").replace("’", "'").replace("‘", "'")
    s = re.sub(r"<0x[0-9A-Fa-f]{2}>", " ", s)  # model writes raw bytes such as a no-break space as <0xC2><0xA0>
    s = re.sub(r"(?<=\w)-\s+(?=\w)", "", s)     # words hyphenated across a line break in the source
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _fragments(evidence: str) -> list[str]:
    """Quoted passages if the model quoted; otherwise the evidence split on ellipses / semicolons."""
    quoted = [a or b for a, b in re.findall(r'"([^"]+)"|“([^”]+)”', evidence)]
    parts = quoted or re.split(r"\.\.\.|…|;|\s/\s", evidence)
    parts = [y for x in parts for y in re.split(r"\.\.\.|…|\s/\s", x)]
    # A single token counts if it is substantial (an amount, an address, a number).
    return [p for p in (_norm(x) for x in parts) if len(p.split()) >= 2 or len(p) >= 3]


def _ngram_share(frag: str, hay: str, n: int = 4) -> float:
    w = frag.split()
    if len(w) <= n:
        return 1.0 if frag in hay else 0.0
    grams = [" ".join(w[i:i + n]) for i in range(len(w) - n + 1)]
    return sum(g in hay for g in grams) / len(grams)


def verify(score: int | None, evidence: str | None, content: str | None, subject: str | None = None) -> str:
    """'' when no evidence is required; else yes / partial / no / missing."""
    if score is None or score < EVIDENCE_REQUIRED_FROM:
        return ""
    frags = _fragments(evidence or "")
    if not frags:
        return MISSING
    hay = _norm((subject or "") + " " + (content or ""))
    if all(f in hay for f in frags):
        return YES
    total = sum(len(f.split()) for f in frags)
    share = sum(_ngram_share(f, hay) * len(f.split()) for f in frags) / total
    return PARTIAL if share >= PARTIAL_THRESHOLD else NO
