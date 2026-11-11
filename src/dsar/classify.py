import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dsar import db, evidence


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

RETRY_PROMPT = (
    "Your evidence is not in the text you were given. Classify the same text again. "
    "Evidence must be copied exactly from that text, never from the examples in your "
    "instructions. If nothing in the text supports a score of 7 or more, score it lower."
)


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
    evidence_retries: int = 0
    failed: int = 0
    failed_chunk_ids: list[str] = field(default_factory=list)


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


def chat_request(model: str, system_prompt: str, messages: list[dict], *,
                 max_tokens: int, temperature: float) -> dict:
    """Body for Ollama's /api/chat. Shared by classify and model-check so both send the same request."""
    return {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
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
    }


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

    base_url = ollama_url()
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
                    resp = await client.post("/api/chat", json=chat_request(
                        model, system_prompt, [{"role": "user", "content": user_prompt}],
                        max_tokens=max_tokens, temperature=temperature))
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
                    # Evidence the model quotes must be in the chunk. If not, ask once
                    # more with the failure stated; keep whichever answer verifies,
                    # else the first. `dsar report` shows the check for every row.
                    if evidence.verify(parsed.score, parsed.evidence, row["content"], row["subject"]) in (evidence.NO, evidence.MISSING):
                        totals.evidence_retries += 1
                        retry = await client.post("/api/chat", json=chat_request(
                            model, system_prompt, [
                                {"role": "user", "content": user_prompt},
                                {"role": "assistant", "content": text},
                                {"role": "user", "content": RETRY_PROMPT},
                            ], max_tokens=max_tokens, temperature=temperature))
                        retry.raise_for_status()
                        rbody = retry.json()
                        rparsed = parse_response(rbody["message"]["content"])
                        input_tokens += _sqlite_safe_int(rbody.get("prompt_eval_count")) or 0
                        output_tokens += _sqlite_safe_int(rbody.get("eval_count")) or 0
                        rcheck = evidence.verify(rparsed.score, rparsed.evidence, row["content"], row["subject"])
                        log.warning("chunk %s: evidence not found in source; retry check=%r", chunk_id, rcheck or "n/a (score < 7)")
                        if rcheck not in (evidence.NO, evidence.MISSING):
                            parsed = rparsed
                        # Keep both answers in raw_response so the retry is on the record.
                        parsed.raw = (f"{text.strip()}\n--- evidence not in source; retried ---\n"
                                      f"{rparsed.raw.strip()}\n--- kept: {'retry' if parsed is rparsed else 'first'} ---")
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
                    totals.failed += 1
                    totals.failed_chunk_ids.append(chunk_id)

        await asyncio.gather(*(worker(cid) for cid in ids), return_exceptions=True)
    return totals


def ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)


def ollama_info(model: str) -> dict:
    """Ollama server version and the model's digest, for the audit log. Never raises."""
    import httpx  # lazy import

    info: dict = {"url": ollama_url(), "version": None, "model": model, "digest": None}
    names = {model, f"{model}:latest"}
    try:
        with httpx.Client(base_url=info["url"], timeout=10.0) as client:
            info["version"] = client.get("/api/version").json().get("version")
            for m in client.get("/api/tags").json().get("models", []):
                if m.get("name") in names or m.get("model") in names:
                    info["digest"] = m.get("digest")
    except Exception as exc:
        info["error"] = str(exc)
    return info


def load_system_prompt(path: str | Path = "claude.md") -> str:
    return Path(path).read_text()
