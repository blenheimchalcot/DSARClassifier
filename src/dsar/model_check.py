"""
`dsar model-check`: test the model on this machine before a case is classified.

Classification can run for days. This finds the problems that would otherwise
show up halfway through: the wrong model build, a context window that silently
truncates chunks, a model that can't hold the scale, or a machine too slow for
the case. It sends the exact request `dsar classify` sends (same system prompt,
user prompt shape and options) and writes nothing to the case database.

Checks:
  golden      synthetic examples, one per band of the 0-10 scale, each scored
              within a tolerance; score 7+ evidence must be in the text
  context     a full-size chunk with the sensitive sentence first, and again
              with it last: both must score 7+, or part of the chunk was lost
  format      every response parses
  determinism the same example twice gives the same answer
  throughput  chunks per minute at the configured concurrency, and the time
              left for this case's pending chunks

The examples are synthetic and never hold case data; the subject's name is
substituted in so the case's own prompt and aliases are exercised.
"""
import asyncio
import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dsar import db, evidence
from dsar.classify import build_user_prompt, chat_request, parse_response
from dsar.config import Config

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RESULT_GLOB = "model_check_*.json"

GOLDEN = [
    {"id": "incidental", "min": 0, "max": 4, "text": (
        "From: Office Manager\nTo: All Staff; {name}; Finance Team\nSubject: Office closure on bank holiday\n\n"
        "Please note the office will be closed on Monday for the bank holiday. Normal hours resume on Tuesday.")},
    {"id": "contextual", "min": 3, "max": 6, "text": (
        "From: Recruitment\nTo: Hiring Managers\nSubject: Interview panel\n\n"
        "Hi both, I was talking to {name} in HR about the interview panel for the analyst role. "
        "We are aiming for Thursday afternoon; please hold the slot.")},
    {"id": "specific-pi", "min": 6, "max": 9, "text": (
        "From: HR Admin\nTo: Payroll\nSubject: Payroll update\n\n"
        "{name}'s new home address is 14 Example Road, Leeds, LS1 4AB, and their date of birth is "
        "3 March 1985. Please update payroll before the next run.")},
    {"id": "behavioural", "min": 7, "max": 10, "text": (
        "From: Team Lead\nTo: Department Head\nSubject: Re: complaint\n\n"
        "{name} has raised yet another grievance and keeps ignoring the agreed process. Their performance "
        "this quarter has been poor and the team is frustrated with their attitude.")},
    {"id": "derogatory", "min": 9, "max": 10, "text": (
        "From: Colleague\nTo: Team Lead\nSubject: Re: Re: team\n\n"
        "Honestly, {name} is useless and a liar. I would not trust a word they say and I want them "
        "out of the team.")},
    {"id": "health", "min": 7, "max": 10, "text": (
        "From: Line Manager\nTo: HR\nSubject: Absence\n\n"
        "{name} has been signed off work for six weeks with depression. Occupational health say they are "
        "on medication and should not return before March.")},
]

CONTEXT_MARKER = ("{name} has been signed off work for six weeks with depression and is on medication, "
                  "according to the occupational health report.")

FILLER = [
    "The facilities team confirmed that the third-floor meeting rooms will be repainted next month. "
    "Bookings should move to the second floor while the work is carried out.",
    "Quarterly figures for the regional offices are attached. Revenue was in line with forecast and "
    "operating costs fell slightly because of lower energy prices.",
    "The IT service desk will upgrade the shared printers over the weekend. Print jobs sent on Friday "
    "evening may need to be resubmitted on Monday morning.",
    "Please remember to submit expense claims by the twentieth of the month so they can be paid in "
    "the next payroll cycle. Late claims roll over to the following month.",
    "The project steering group agreed the revised timeline. Phase two now starts in the spring, "
    "with user testing planned for early summer.",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(cfg: Config, system_prompt: str, digest: str | None) -> dict:
    """What a passing check vouches for. Any change needs a new check."""
    return {
        "model": cfg.model.default,
        "digest": digest,
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "max_tokens": cfg.model.max_tokens,
        "temperature": cfg.model.temperature,
    }


def context_text(name: str, max_chars: int, marker_first: bool) -> str:
    marker = CONTEXT_MARKER.format(name=name)
    paras, size, i = [], len(marker), 0
    while size < max_chars - 300:
        p = FILLER[i % len(FILLER)]
        paras.append(p)
        size += len(p) + 2
        i += 1
    body = [marker, *paras] if marker_first else [*paras, marker]
    return "From: HR\nTo: Line Managers\nSubject: Monthly update\n\n" + "\n\n".join(body)


class _Model:
    """One configured model on one Ollama server, called exactly as classify calls it."""

    def __init__(self, client, cfg: Config, system_prompt: str):
        self.client, self.cfg, self.system_prompt = client, cfg, system_prompt

    async def classify(self, text: str, source_type: str = "email", source_path: str = "model-check") -> dict:
        user = build_user_prompt(source_type, source_path, self.cfg.subject.name, text)
        t0 = time.monotonic()
        resp = await self.client.post("/api/chat", json=chat_request(
            self.cfg.model.default, self.system_prompt, [{"role": "user", "content": user}],
            max_tokens=self.cfg.model.max_tokens, temperature=self.cfg.model.temperature))
        resp.raise_for_status()
        body = resp.json()
        parsed = parse_response(body["message"]["content"])
        return {
            "score": parsed.score,
            "special_return": parsed.special_return,
            "evidence": parsed.evidence,
            "evidence_in_source": evidence.verify(parsed.score, parsed.evidence, text, None),
            "raw": parsed.raw.strip(),
            "seconds": round(time.monotonic() - t0, 2),
            "prompt_tokens": body.get("prompt_eval_count"),
            "parsed": parsed.score is not None or parsed.special_return is not None,
        }


def _evidence_ok(r: dict) -> bool:
    return r["score"] is None or r["score"] < evidence.EVIDENCE_REQUIRED_FROM or \
        r["evidence_in_source"] in (evidence.YES, evidence.PARTIAL)


async def _run(model: _Model, cfg: Config, sample: list[dict], pending: int | None) -> dict:
    name = cfg.subject.name
    golden = []
    for g in GOLDEN:
        r = await model.classify(g["text"].format(name=name), source_path=f"model-check/{g['id']}")
        in_band = r["score"] is not None and g["min"] <= r["score"] <= g["max"]
        golden.append({"id": g["id"], "band": [g["min"], g["max"]], **r,
                       "pass": in_band and _evidence_ok(r)})

    context = []
    for first in (True, False):
        text = context_text(name, cfg.chunking.max_chars, marker_first=first)
        r = await model.classify(text, source_path="model-check/context")
        ok = r["score"] is not None and r["score"] >= evidence.EVIDENCE_REQUIRED_FROM and _evidence_ok(r)
        context.append({"marker": "first" if first else "last", "chars": len(text), **r, "pass": ok})

    repeat = await model.classify(GOLDEN[3]["text"].format(name=name), source_path=f"model-check/{GOLDEN[3]['id']}")
    deterministic = repeat["raw"] == golden[3]["raw"]

    sem = asyncio.Semaphore(cfg.model.concurrency)

    async def timed(item: dict) -> dict:
        async with sem:
            return await model.classify(item["text"], item.get("source_type", "email"), item.get("source_path", "model-check"))

    t0 = time.monotonic()
    timed_results = await asyncio.gather(*(timed(s) for s in sample))
    elapsed = time.monotonic() - t0
    rate = len(timed_results) / (elapsed / 60) if elapsed > 0 else 0.0

    every = golden + context + [repeat] + list(timed_results)
    return {
        "golden": golden,
        "context": context,
        "deterministic": deterministic,
        "throughput": {
            "sample_size": len(timed_results),
            "sample_source": sample[0].get("kind", "golden") if sample else "none",
            "concurrency": cfg.model.concurrency,
            "seconds": round(elapsed, 1),
            "chunks_per_minute": round(rate, 2),
            "mean_seconds_per_chunk": round(sum(r["seconds"] for r in timed_results) / len(timed_results), 2)
            if timed_results else None,
            "pending_chunks": pending,
            "estimated_hours": round(pending / rate / 60, 1) if pending and rate else None,
        },
        "format_rate": round(sum(r["parsed"] for r in every) / len(every), 3),
        "responses": len(every),
    }


def sample_chunks(conn: sqlite3.Connection | None, in_scope: Callable[[str], bool] | None,
                  n: int, seed: int = 0) -> tuple[list[dict], int | None]:
    """Pending in-scope chunks to time (real lengths and shapes); golden texts if none yet."""
    pending_ids = db.unclassified_chunk_ids(conn, in_scope=in_scope) if conn is not None else []
    if pending_ids:
        rng = random.Random(seed)
        chosen = rng.sample(pending_ids, min(n, len(pending_ids)))
        out = []
        for cid in chosen:
            row = db.get_chunk(conn, cid)
            out.append({"kind": "case chunks", "text": row["content"], "source_type": row["source_type"],
                        "source_path": row["source_path"]})
        return out, len(pending_ids)
    return [{"kind": "golden", "text": GOLDEN[i % len(GOLDEN)]["text"]} for i in range(n)], None


def evaluate(results: dict, info: dict, loaded: dict | None, cfg: Config) -> list[dict]:
    s = cfg.model_check
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    if info.get("digest"):
        add("Model", PASS, f"{info['model']} digest {info['digest'][:12]}, Ollama {info.get('version')}")
    else:
        add("Model", FAIL, f"{info['model']} not found on {info.get('url')}: {info.get('error', 'not installed')}")
    expected = (cfg.model.expected_digest or "").lower()
    if expected and info.get("digest") and not info["digest"].lower().startswith(expected):
        add("Model digest", FAIL, f"installed {info['digest'][:12]}, config expects {expected}")

    passed = sum(g["pass"] for g in results["golden"])
    accuracy = passed / len(results["golden"])
    misses = [f"{g['id']} scored {g['score'] if g['score'] is not None else g['special_return']} "
              f"(band {g['band'][0]}-{g['band'][1]}"
              + (", evidence not in text" if not _evidence_ok(g) else "") + ")"
              for g in results["golden"] if not g["pass"]]
    add("Accuracy", PASS if accuracy >= s.min_accuracy else FAIL,
        f"{passed}/{len(results['golden'])} golden examples in band (min {s.min_accuracy:.0%})"
        + (": " + "; ".join(misses) if misses else ""))

    lost = [c["marker"] for c in results["context"] if not c["pass"]]
    ctx_len = (loaded or {}).get("context_length")
    ctx_note = f"; server context {ctx_len} tokens" if ctx_len else ""
    if lost:
        add("Context window", FAIL,
            f"sensitive sentence placed {' and '.join(lost)} in a {cfg.chunking.max_chars}-char chunk was missed: "
            f"chunks are being truncated (OLLAMA_CONTEXT_LENGTH=8192){ctx_note}")
    else:
        add("Context window", PASS, f"full {cfg.chunking.max_chars}-char chunk seen, start and end{ctx_note}")

    add("Format", PASS if results["format_rate"] >= s.min_format_rate else FAIL,
        f"{results['format_rate']:.0%} of {results['responses']} responses parsed (min {s.min_format_rate:.0%})")
    add("Determinism", PASS if results["deterministic"] else WARN,
        "same answer twice" if results["deterministic"]
        else f"two runs of the same example differed at temperature {cfg.model.temperature}")

    t = results["throughput"]
    eta = f"; {t['pending_chunks']} pending chunks ≈ {t['estimated_hours']} h" if t["estimated_hours"] else ""
    add("Throughput", PASS if t["chunks_per_minute"] >= s.min_chunks_per_minute else WARN,
        f"{t['chunks_per_minute']} chunks/min at concurrency {t['concurrency']} on {t['sample_size']} "
        f"{t['sample_source']}{eta}")
    if loaded and loaded.get("size"):
        vram = loaded.get("size_vram") or 0
        add("Memory", PASS, f"model uses {loaded['size'] / 1024 ** 3:.1f} GiB, "
                            f"{vram / loaded['size']:.0%} on GPU")
    return checks


async def _with_client(make_client: Callable, cfg: Config, system_prompt: str,
                       sample: list[dict], pending: int | None) -> dict:
    async with make_client() as client:
        return await _run(_Model(client, cfg, system_prompt), cfg, sample, pending)


def run(make_client: Callable, cfg: Config, system_prompt: str, info: dict, get_loaded: Callable[[], dict | None],
        conn: sqlite3.Connection | None = None, in_scope: Callable[[str], bool] | None = None) -> dict:
    """Run every check; return the result document (written to output/ by the caller).

    `make_client()` returns an httpx.AsyncClient for the Ollama server.
    """
    sample, pending = sample_chunks(conn, in_scope, cfg.model_check.sample_size)
    started = _now()
    results = asyncio.run(_with_client(make_client, cfg, system_prompt, sample, pending))
    checks = evaluate(results, info, get_loaded(), cfg)
    passed = not any(c["status"] == FAIL for c in checks)
    # Timed case chunks stay out of the result file: their responses describe the subject.
    return {
        "started_at": started,
        "finished_at": _now(),
        "passed": passed,
        "fingerprint": fingerprint(cfg, system_prompt, info.get("digest")),
        "ollama": info,
        "checks": checks,
        "golden": results["golden"],
        "context": [{k: v for k, v in c.items() if k != "raw"} for c in results["context"]],
        "deterministic": results["deterministic"],
        "throughput": results["throughput"],
        "format_rate": results["format_rate"],
    }


def write_result(result: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"model_check_{stamp}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def latest_pass(output_dir: Path, fp: dict) -> Path | None:
    """Newest passing model check for exactly this model build, prompt and settings."""
    for path in sorted(output_dir.glob(RESULT_GLOB), reverse=True):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("fingerprint") == fp:
            return path if doc.get("passed") else None
    return None
