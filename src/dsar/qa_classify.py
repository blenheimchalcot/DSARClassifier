"""
`dsar qa classify`: validate the model's personal-data classifications.

`verify-evidence` catches quotes the model invented; it cannot catch a real
quote carrying the wrong score, or personal data the model scored too low.
These checks look for both, and give a human reviewer a fair sample to mark:

  format       responses that are neither a score nor a special return (FAIL above
               qa.max_unparseable_rate)
  coverage     in-scope chunks still unclassified (WARN)
  evidence     score 7+ whose quote is not (fully) in the source (WARN, per row)
  special      score < 5 but the text mentions health, criminal or other
               special-category matters (WARN, per row: possible missed PI)
  distribution scores per source type and custodian; groups whose share of 7+
               is far above the case's (INFO)
  consistency a random sample re-scored with the same request; FAIL if fewer
               than qa.min_rescore_agreement land within ±1 of the original
  sample       a stratified sample per score band written to an Excel workbook
               for a human to score; `dsar qa import-sample` measures agreement

Everything goes to output/ and the audit log. Model scores are never changed.
"""
import asyncio
import csv
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dsar import evidence
from dsar.config import Config

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
BANDS = ("0-4", "5-6", "7-10", "special")
HIGH = evidence.EVIDENCE_REQUIRED_FROM

# Special categories (UK GDPR Art. 9/10) and other sensitive matters. A low score on
# text mentioning these is worth a human look; the match itself proves nothing.
SPECIAL_TERMS = re.compile(
    r"\b(sick(ness)?\s+(leave|note)|signed\s+off|illness|diagnos\w*|medical|medication|depress\w*|anxiety|"
    r"mental\s+health|occupational\s+health|disabilit\w*|pregnan\w*|maternity|cancer|surgery|therapy|"
    r"counsell?ing|grievance|disciplinary|dismiss\w*|arrest\w*|convict\w*|criminal|police|offence|"
    r"trade\s+union|religio\w*|ethnic\w*|sexual\s+orientation|transgender|gender\s+identity|"
    r"political\s+(opinion|views)|biometric|genetic)\b", re.IGNORECASE)

CLASSIFIED_SQL = """
SELECT cl.chunk_id, cl.score, cl.description, cl.evidence, cl.special_return, cl.raw_response,
       c.source_type, c.source_path, c.subject, c.content,
       COALESCE(NULLIF(pf.purview_custodian, ''), '(unknown)') AS custodian
FROM classifications cl
JOIN chunks c ON c.chunk_id = cl.chunk_id
LEFT JOIN provenance_files pf ON pf.source_path = c.source_path
"""


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def band(score: int | None, special: str | None) -> str:
    if score is None:
        return "special"
    return "7-10" if score >= HIGH else "5-6" if score >= 5 else "0-4"


def _excerpt(text: str, match: re.Match, width: int = 80) -> str:
    start, end = max(0, match.start() - width), min(len(text), match.end() + width)
    return " ".join(text[start:end].split())


def analyse(conn: sqlite3.Connection, cfg: Config, in_scope: Callable[[str], bool] | None = None) -> dict:
    rows = [dict(r) for r in conn.execute(CLASSIFIED_SQL)]
    findings: list[dict] = []

    def add(check, severity, row, detail):
        findings.append({"check": check, "severity": severity, "chunk_id": row.get("chunk_id", ""),
                         "score": row.get("score"), "source_path": row.get("source_path", ""), "detail": detail})

    # format
    unparseable = [r for r in rows if r["score"] is None and not r["special_return"]]
    specials = Counter(r["special_return"] for r in rows if r["special_return"])
    rate = len(unparseable) / len(rows) if rows else 0.0
    for r in unparseable:
        add("format", WARN, r, f"unparseable response: {(r['raw_response'] or '')[:200]!r}")

    # coverage
    pending = conn.execute(
        "SELECT c.source_path FROM chunks c LEFT JOIN classifications cl ON cl.chunk_id = c.chunk_id "
        "WHERE c.alias_matched = 1 AND c.skipped_as_duplicate_of IS NULL AND cl.chunk_id IS NULL").fetchall()
    pending_in_scope = sum(1 for (sp,) in pending if in_scope is None or in_scope(sp))

    # evidence + special categories
    evidence_counts: Counter = Counter()
    special_hits = 0
    for r in rows:
        if r["score"] is not None and r["score"] >= HIGH:
            v = evidence.verify(r["score"], r["evidence"], r["content"], r["subject"])
            evidence_counts[v] += 1
            if v != evidence.YES:
                add("evidence", WARN, r, f"evidence_in_source={v}: {(r['evidence'] or '')[:200]!r}")
        elif r["score"] is not None and r["score"] < 5:
            m = SPECIAL_TERMS.search(r["content"] or "")
            if m:
                special_hits += 1
                add("special", WARN, r, f"scored {r['score']} but mentions '{m.group(0)}': …{_excerpt(r['content'], m)}…")

    # distribution
    def histogram(key: str) -> dict:
        out: dict = defaultdict(Counter)
        for r in rows:
            out[r[key]][str(r["score"]) if r["score"] is not None else (r["special_return"] or "unparseable")] += 1
        return {k: dict(v) for k, v in out.items()}

    scored = [r for r in rows if r["score"] is not None]
    overall_high = sum(r["score"] >= HIGH for r in scored) / len(scored) if scored else 0.0
    groups: dict = defaultdict(list)
    for r in scored:
        groups[("custodian", r["custodian"])].append(r["score"])
        groups[("source_type", r["source_type"])].append(r["score"])
    for (kind, name), scores in groups.items():
        share = sum(s >= HIGH for s in scores) / len(scores)
        if len(scores) >= 20 and share > 0.2 and share > 3 * overall_high:
            findings.append({"check": "distribution", "severity": INFO, "chunk_id": "", "score": None,
                             "source_path": "", "detail": f"{kind} '{name}': {share:.0%} scored {HIGH}+ "
                                                          f"vs {overall_high:.0%} overall ({len(scores)} chunks)"})

    q = cfg.qa
    checks = [
        {"name": "Format", "status": FAIL if rate > q.max_unparseable_rate else PASS,
         "detail": f"{len(unparseable)} of {len(rows)} responses unparseable ({rate:.1%}, max {q.max_unparseable_rate:.0%}); "
                   f"special returns {dict(specials)}"},
        {"name": "Coverage", "status": WARN if pending_in_scope else PASS,
         "detail": f"{pending_in_scope} in-scope chunks not yet classified"},
        {"name": "Evidence", "status": WARN if sum(v for k, v in evidence_counts.items() if k != evidence.YES) else PASS,
         "detail": f"score {HIGH}+: {dict(evidence_counts)}"},
        {"name": "Special categories", "status": WARN if special_hits else PASS,
         "detail": f"{special_hits} chunks scored < 5 mention special-category or sensitive terms"},
    ]
    return {
        "rows": rows,
        "findings": findings,
        "checks": checks,
        "stats": {
            "classified": len(rows),
            "pending_in_scope": pending_in_scope,
            "bands": dict(Counter(band(r["score"], r["special_return"]) for r in rows)),
            "high_share": round(overall_high, 3),
            "by_source_type": histogram("source_type"),
            "by_custodian": histogram("custodian"),
        },
    }


async def _rescore(make_client: Callable, cfg: Config, system_prompt: str, sample: list[dict]) -> list[dict]:
    from dsar.model_check import ModelClient

    async with make_client() as client:
        model = ModelClient(client, cfg, system_prompt)
        sem = asyncio.Semaphore(cfg.model.concurrency)

        async def one(r: dict) -> dict:
            async with sem:
                try:
                    again = await model.classify(r["content"], r["source_type"], r["source_path"], r["subject"])
                    return {"chunk_id": r["chunk_id"], "original": r["score"], "rescored": again["score"],
                            "special_return": again["special_return"]}
                except Exception as exc:
                    return {"chunk_id": r["chunk_id"], "original": r["score"], "rescored": None, "error": str(exc)}

        return await asyncio.gather(*(one(r) for r in sample))


def consistency(make_client: Callable, cfg: Config, system_prompt: str, rows: list[dict], n: int,
                seed: int = 0) -> tuple[dict, list[dict]]:
    scored = [r for r in rows if r["score"] is not None]
    sample = random.Random(seed).sample(scored, min(n, len(scored)))
    if not sample:
        return {"name": "Consistency", "status": WARN, "detail": "no scored classifications to re-score"}, []
    results = asyncio.run(_rescore(make_client, cfg, system_prompt, sample))
    usable = [r for r in results if r["rescored"] is not None]
    exact = sum(r["rescored"] == r["original"] for r in usable)
    near = sum(abs(r["rescored"] - r["original"]) <= 1 for r in usable)
    flips = sum((r["rescored"] >= HIGH) != (r["original"] >= HIGH) for r in usable)
    agreement = near / len(results)
    status = PASS if agreement >= cfg.qa.min_rescore_agreement else FAIL
    return ({"name": "Consistency", "status": status,
             "detail": f"{len(results)} re-scored: {exact} identical, {near} within ±1 ({agreement:.0%}, min "
                       f"{cfg.qa.min_rescore_agreement:.0%}), {flips} crossed the {HIGH}+ line, "
                       f"{len(results) - len(usable)} failed"}, results)


def stratified_sample(rows: list[dict], per_band: int, seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    by_band: dict = defaultdict(list)
    for r in rows:
        by_band[band(r["score"], r["special_return"])].append(r)
    out = []
    for b in BANDS:
        pool = sorted(by_band.get(b, []), key=lambda r: r["chunk_id"])
        out += [{**r, "band": b} for r in rng.sample(pool, min(per_band, len(pool)))]
    return out


SAMPLE_COLUMNS = ["chunk_id", "band", "model_score", "model_description", "model_evidence", "evidence_in_source",
                  "source_type", "source_path", "subject", "content",
                  "reviewer_score", "reviewer_initials", "reviewer_notes"]
SAMPLE_INSTRUCTIONS = [
    "Quality-assurance sample of the model's classifications. Do not change columns A-J.",
    "For each row, read the content and enter your own score (0-10) in reviewer_score, using the same scale "
    "the model used. Leave model_score out of your mind: score as if you had not seen it.",
    "If the row is a special return ([not able to classify], [Email empty], [File empty]) and you agree, "
    "enter -1. Otherwise enter the score you would give.",
    "Enter your initials in reviewer_initials. Add notes for anything the model clearly missed or over-scored.",
    "Return the workbook to the operator, who runs `dsar qa import-sample <file>`.",
]


def write_sample(sample: list[dict], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Sample"
    ws.append(SAMPLE_COLUMNS)
    for r in sample:
        ws.append([r["chunk_id"], r["band"], r["score"] if r["score"] is not None else r["special_return"],
                   r["description"] or "", r["evidence"] or "",
                   evidence.verify(r["score"], r["evidence"], r["content"], r["subject"]),
                   r["source_type"], r["source_path"], r["subject"] or "", (r["content"] or "")[:32000],
                   None, None, None])
    header_fill = PatternFill("solid", fgColor="DDDDDD")
    input_fill = PatternFill("solid", fgColor="FFF2CC")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for cell in row[10:13]:
            cell.fill = input_fill
    for col, width in zip("ABCDEFGHIJKLM", (18, 8, 11, 30, 30, 10, 12, 30, 25, 90, 12, 12, 30)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "C2"
    dv = DataValidation(type="whole", operator="between", formula1="-1", formula2="10", allow_blank=True,
                        showErrorMessage=True, error="Enter a whole number from 0 to 10 (or -1 to agree with a special return)")
    ws.add_data_validation(dv)
    dv.add(f"K2:K{max(2, len(sample) + 1)}")
    info = wb.create_sheet("Instructions")
    for line in SAMPLE_INSTRUCTIONS:
        info.append([line])
    info.column_dimensions["A"].width = 120
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def import_sample(path: Path) -> dict:
    """Agreement between reviewer and model on a returned QA workbook."""
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True)["Sample"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    idx = {name: header.index(name) for name in SAMPLE_COLUMNS}
    per_band: dict = defaultdict(lambda: {"reviewed": 0, "exact": 0, "within_1": 0, "same_side_of_7": 0})
    outstanding, reviewers, disagreements = 0, set(), []
    for row in rows[1:]:
        if row[idx["chunk_id"]] is None:
            continue
        reviewer = row[idx["reviewer_score"]]
        if reviewer is None or str(reviewer).strip() == "":
            outstanding += 1
            continue
        reviewer = int(reviewer)
        model = row[idx["model_score"]]
        b = row[idx["band"]]
        stats = per_band[b]
        stats["reviewed"] += 1
        if row[idx["reviewer_initials"]]:
            reviewers.add(str(row[idx["reviewer_initials"]]))
        if not isinstance(model, int):        # special return: -1 means the reviewer agrees
            agree = reviewer == -1
            stats["exact"] += agree
            stats["within_1"] += agree
            stats["same_side_of_7"] += agree or reviewer < HIGH
        else:
            stats["exact"] += reviewer == model
            stats["within_1"] += abs(reviewer - model) <= 1
            stats["same_side_of_7"] += (reviewer >= HIGH) == (model >= HIGH)
        if not isinstance(model, int) and reviewer != -1 or isinstance(model, int) and abs(reviewer - model) > 1:
            disagreements.append({"chunk_id": row[idx["chunk_id"]], "model": model, "reviewer": reviewer,
                                  "notes": row[idx["reviewer_notes"]] or ""})
    total = {k: sum(s[k] for s in per_band.values()) for k in ("reviewed", "exact", "within_1", "same_side_of_7")}
    rate = (lambda k: round(total[k] / total["reviewed"], 3) if total["reviewed"] else None)
    return {
        "workbook": path.name,
        "reviewed": total["reviewed"],
        "outstanding": outstanding,
        "reviewers": sorted(reviewers),
        "agreement_exact": rate("exact"),
        "agreement_within_1": rate("within_1"),
        "agreement_same_side_of_7": rate("same_side_of_7"),
        "per_band": {b: dict(s) for b, s in per_band.items()},
        "disagreements": disagreements,
    }


def write_outputs(result: dict, checks: list[dict], rescore: list[dict], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    when = stamp()
    csv_path = output_dir / f"qa_classify_{when}.csv"
    fields = ["check", "severity", "chunk_id", "score", "source_path", "detail"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(result["findings"])
    json_path = output_dir / f"qa_classify_{when}.json"
    json_path.write_text(json.dumps({
        "passed": not any(c["status"] == FAIL for c in checks),
        "checks": checks,
        "stats": result["stats"],
        "rescore": rescore,
        "findings_csv": csv_path.name,
    }, indent=2, default=str), encoding="utf-8")
    return json_path, csv_path
