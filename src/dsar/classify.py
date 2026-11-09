import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dsar import db


log = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"

_SQLITE_INT_MAX = 2**63 - 1
_SQLITE_INT_MIN = -(2**63)


def _sqlite_safe_int(v) -> int | None:
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if _SQLITE_INT_MIN <= v <= _SQLITE_INT_MAX:
        return v
    return None


_SPECIAL_RETURNS = {"[not able to classify]", "[Email empty]", "[File empty]"}


@dataclass
class ParsedResponse:
    score: int | None
    description: str | None
    evidence: str | None
    special_return: str | None
    raw: str


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    requests: int = 0


def parse_response(raw: str) -> ParsedResponse:
    """Parse a response produced by the prompt in claude.md (pipe-delimited)."""
    text = raw.strip()
    # Take the first non-empty line so chatty prefaces don't break us.
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")

    for special in _SPECIAL_RETURNS:
        if special.lower() in first.lower():
            return ParsedResponse(None, None, None, special, raw)

    parts = [p.strip() for p in first.split("|")]
    score: int | None = None
    description: str | None = None
    evidence: str | None = None

    if parts:
        m = re.match(r"^\**\s*(\d+)", parts[0])
        if m:
            try:
                score = int(m.group(1))
            except ValueError:
                score = None
            # Anything outside the documented 0-10 scale is a model hallucination
            # (one case observed: a 25-digit number that overflowed SQLite's
            # signed-64-bit INTEGER); treat as unparseable.
            if score is not None and not 0 <= score <= 10:
                score = None
    if len(parts) >= 2:
        description = parts[1] or None
    if len(parts) >= 3:
        evidence = parts[2] or None

    # Per claude.md: evidence only when score >= 7.
    if score is None or score < 7:
        evidence = None

    return ParsedResponse(score, description, evidence, None, raw)


def build_user_prompt(source_type: str, source_path: str, subject_name: str, context: str) -> str:
    return (
        f"Classify the following {source_type} (Source: {source_path}) "
        f"regarding Data Subject {subject_name} using the defined 0-10 scale:\n"
        f"{context}"
    )


async def classify_all(
    conn: sqlite3.Connection,
    *,
    system_prompt: str,
    model: str,
    subject_name: str,
    max_tokens: int,
    temperature: float,
    concurrency: int,
    limit: int | None = None,
    progress: Callable | None = None,
    in_scope: Callable[[str], bool] | None = None,
) -> UsageTotals:
    import httpx  # lazy import

    base_url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    ids = db.unclassified_chunk_ids(conn, limit=limit, in_scope=in_scope)
    totals = UsageTotals()
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base_url, timeout=300.0) as client:
        async def worker(chunk_id: str) -> None:
            async with sem:
                try:
                    row = db.get_chunk(conn, chunk_id)
                    if row is None:
                        return
                    user_prompt = build_user_prompt(
                        source_type=row["source_type"],
                        source_path=row["source_path"],
                        subject_name=subject_name,
                        context=row["content"],
                    )
                    resp = await client.post("/api/chat", json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "stream": False,
                        # Gemma 4 (and other reasoning-capable models) emit hidden
                        # `thinking` tokens by default, which consume the budget
                        # before any visible content is produced. We want a single
                        # pipe-delimited line, not chain-of-thought.
                        "think": False,
                        "options": {
                            "temperature": temperature,
                            "num_predict": max_tokens,
                        },
                    })
                    resp.raise_for_status()
                    body = resp.json()
                    text = body["message"]["content"]
                    parsed = parse_response(text)
                    # Ollama occasionally returns token counts outside SQLite's
                    # signed-64-bit INTEGER range (a negative uint wrapped, or
                    # similar) — clamp to None rather than letting the diagnostic
                    # field block the actual classification.
                    input_tokens = _sqlite_safe_int(body.get("prompt_eval_count")) or 0
                    output_tokens = _sqlite_safe_int(body.get("eval_count")) or 0
                    db.insert_classification(
                        conn,
                        chunk_id=chunk_id,
                        score=parsed.score,
                        description=parsed.description,
                        evidence=parsed.evidence,
                        special_return=parsed.special_return,
                        raw_response=parsed.raw,
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=0,  # Ollama caches the system prompt transparently across requests.
                    )
                    conn.commit()
                    totals.requests += 1
                    totals.input_tokens += input_tokens
                    totals.output_tokens += output_tokens
                    if progress is not None:
                        progress(chunk_id, parsed, totals)
                except Exception as exc:
                    # One slow/failed request must not kill the whole run. Ollama can
                    # return 500 on its internal 5-min ceiling, and httpx times out
                    # at 300s; either propagates through gather() and cancels every
                    # other worker. Log and move on — the chunk stays unclassified
                    # and can be retried by re-running `dsar classify`.
                    log.warning("classify failed for chunk %s: %s", chunk_id, exc)

        await asyncio.gather(*(worker(cid) for cid in ids), return_exceptions=True)
    return totals


def load_system_prompt(path: str | Path = "claude.md") -> str:
    return Path(path).read_text()
