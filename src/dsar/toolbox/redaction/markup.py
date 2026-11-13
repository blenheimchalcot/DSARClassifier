"""
Mark up documents for release: approve passages, redact others, export a redacted copy.

`dsar markup serve FOLDER` serves the .txt files in FOLDER on 127.0.0.1 only. The
reviewer decides each element (a transcript turn, or a paragraph or header block
of any other file): release it, redact all of it, or redact parts of it, with a
reason for each redaction. The page switches between the original and the
redacted view. Every action is appended to FOLDER/markup_events.jsonl,
hash-chained like the audit log; the current state is the replay of those events.
Positions are character offsets into the file, checked against its SHA-256 so a
mark can never drift onto other text.

`dsar markup score FOLDER` has the local model suggest a 0-10 score for every
element, with the case prompt and scale, into FOLDER/markup_scores.jsonl. The page
shows it as a hint beside each element; it is never recorded as a decision.

"Export Personal Data extracts" collects every released passage across the
folder into plain report text: one extract per passage, identical extracts
merged, no headings or sources. A private register beside it lists every
source of each extract, for the case record only.

The export page is a true redaction: redacted text is removed before the page is
built, never hidden with styling. Printing it to PDF gives the release copy, and
each export is saved next to the files and logged in the case audit log.
Nothing leaves the machine; the page loads no external resources.
"""
import csv
import hashlib
import html
import json
import re
import secrets
import socketserver
import threading
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dsar import audit

EVENTS_NAME = "markup_events.jsonl"
SCORES_NAME = "markup_scores.jsonl"
EXPORT_DIR = "redacted_exports"
EXTRACT_DIR = "personal_data_extracts"
GENESIS = "0" * 64
REASONS = [
    "Third-party personal data",
    "Not the requester's personal data",
    "Legal professional privilege",
    "Confidential reference",
    "Duplicate – already in the Personal Data Report",
    "Other (see note)",                 # keep last: it needs a note
]
# A speaker name may be broken across one line break ("Firstname\nSurname:").
_TURN = re.compile(r"(\d\d:\d\d:\d\d)(?:\.\d+)?\s*-\s*[\d:.]+\s+([^:\n]{1,40}(?:\n[^:\n]{1,40})?):[ \t]*")
# The first version's pattern, kept only to place decisions recorded under it (see Folder.docs).
_TURN_V1 = re.compile(r"(\d\d:\d\d:\d\d)(?:\.\d+)?\s*-\s*[\d:.]+\s+([^:\n]{2,60}):[ \t]*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse(text: str, splits: list[dict] | None = None, _turn: re.Pattern = _TURN, _positional: bool = False) -> dict:
    """Split a file into elements, each made of (start, end) slices of the original text.

    A Call AI transcript gives the meeting details, then one element per
    uninterrupted speaker turn. Any other file gives its header block (an email's
    Subject/From/To lines) and then one element per paragraph. Each element's id is
    its start offset, so fixing or splitting one element never renames another.
    A reviewer's splits ({"at": offset, "who": speaker}) then cut elements in two.
    """
    heads = list(_turn.finditer(text))
    if len(heads) >= 2 and "Transcript" in text[: heads[0].start()]:
        kind = "transcript"
        elements = [{"label": "Meeting details", "who": None, "t": None, "parts": [[0, heads[0].start()]]}]
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            who = " ".join(h.group(2).split())
            if elements[-1]["who"] == who:
                elements[-1]["parts"].append([h.end(), end])
            else:
                elements.append({"label": who, "who": who, "t": h.group(1), "parts": [[h.end(), end]]})
    else:
        kind, blocks, pos = "text", [], 0
        for m in re.finditer(r"\n[ \t]*\n", text):
            if text[pos:m.start()].strip():
                blocks.append([pos, m.start()])
            pos = m.end()
        if text[pos:].strip():
            blocks.append([pos, len(text)])
        first = text[blocks[0][0]:blocks[0][1]] if blocks else ""
        header = bool(first) and all(re.match(r"^[A-Za-z-]+: ", ln) for ln in first.splitlines() if ln.strip())
        elements = [{"label": "Header" if header and i == 0 else f"Paragraph {i + (0 if header else 1)}",
                     "who": None, "t": None, "parts": [b]} for i, b in enumerate(blocks)]
    for s in sorted(splits or [], key=lambda s: s["at"]):
        for i, el in enumerate(elements):
            j = next((j for j, (a, b) in enumerate(el["parts"]) if a < s["at"] < b), None)
            if j is None:
                continue
            a, b = el["parts"][j]
            who = s.get("who") or el["who"]
            new = {"label": who or el["label"], "who": who, "t": None, "split": True,
                   "parts": [[s["at"], b]] + el["parts"][j + 1:]}
            el["parts"] = el["parts"][:j] + [[a, s["at"]]]
            elements.insert(i + 1, new)
            break
    for i, el in enumerate(elements):
        el["id"] = f"e{i}" if _positional else f"e{el['parts'][0][0]}"
    return {"kind": kind, "elements": elements}


def _overlaps(x: list, y: list) -> bool:
    return any(a < d and c < b for a, b in x for c, d in y)


class Events:
    """Append-only, hash-chained log of markup actions in one folder."""

    # One lock per events file, shared by every Events object for it: the web app
    # builds a Folder per request, and two appends must never read the same tail.
    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, folder: Path):
        self.path = Path(folder) / EVENTS_NAME
        with Events._guard:
            self._lock = Events._locks.setdefault(str(self.path.resolve()), threading.Lock())

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def append(self, event: dict) -> dict:
        with self._lock:
            lines = self.lines()
            prev = hashlib.sha256(lines[-1].encode()).hexdigest() if lines else GENESIS
            entry = {**event, "seq": len(lines), "ts": _now(), "prev": prev}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        return entry

    def verify(self) -> bool:
        prev = GENESIS
        for ln in self.lines():
            if json.loads(ln)["prev"] != prev:
                return False
            prev = hashlib.sha256(ln.encode()).hexdigest()
        return True

    def for_file(self, name: str, sha: str) -> list[dict]:
        """This file's events, oldest first; events made on another version of it are left out."""
        return [e for e in map(json.loads, self.lines()) if e["file"] == name and e["sha256"] == sha]


def redacted_text(text: str, start: int, end: int, redactions: list[dict]) -> list[tuple[str, dict | None]]:
    """Pieces of text[start:end] with redacted ranges replaced: (text, None) or ("", redaction)."""
    pieces, pos = [], start
    for r in sorted(redactions, key=lambda r: r["start"]):
        a, b = max(r["start"], start), min(r["end"], end)
        if b <= a or a < pos:
            continue
        if a > pos:
            pieces.append((text[pos:a], None))
        pieces.append(("", r))
        pos = b
    if pos < end:
        pieces.append((text[pos:end], None))
    return pieces


class Folder:
    def __init__(self, folder: Path, reviewer: str, case_output: Path | None):
        self.folder = Path(folder)
        self.reviewer = reviewer
        self.case_output = case_output
        self.events = Events(self.folder)

    def files(self) -> list[Path]:
        return sorted(p for p in self.folder.glob("*.txt") if p.is_file())

    def file(self, name: str) -> Path:
        path = self.folder / name
        if path.parent != self.folder or path.suffix != ".txt" or not path.is_file():
            raise KeyError(name)
        return path

    @staticmethod
    def _splits(events: list[dict]) -> list[dict]:
        splits: dict = {}
        for e in events:
            if e["action"] == "split":
                splits[e["at"]] = {"at": e["at"], "who": e.get("who")}
            elif e["action"] == "unsplit":
                splits.pop(e["at"], None)
        return list(splits.values())

    def resolve(self, text: str, events: list[dict]) -> dict:
        """Elements and their current decisions.

        A decision applies to an element only if it was made on exactly that span of
        text. Where an element has been split, or the parser corrected, since the
        decision was made, it is shown as 'earlier decision' and must be made again.
        """
        doc = parse(text, self._splits(events))
        v1 = None
        active: list[dict] = []                     # decisions still in force, each with the span it covered
        for e in events:
            if e["action"] not in ("decide", "reset"):
                continue
            span = e.get("span")
            if span is None:                        # recorded before ids were offsets: place it via the first parser
                v1 = v1 or {x["id"]: x["parts"] for x in parse(text, _turn=_TURN_V1, _positional=True)["elements"]}
                span = v1.get(e["element"])
                if span is None:
                    continue
            active = [d for d in active if not _overlaps(d["span"], span)]
            if e["action"] == "decide":
                active.append({**{k: e[k] for k in ("decision", "redactions", "reviewer", "ts")}, "span": span})
        decisions, earlier = {}, {}
        for el in doc["elements"]:
            hits = [d for d in active if _overlaps(d["span"], el["parts"])]
            if len(hits) == 1 and hits[0]["span"] == el["parts"]:
                decisions[el["id"]] = hits[0]
            elif hits:
                earlier[el["id"]] = [{k: d[k] for k in ("decision", "reviewer", "ts")} |
                                     {"reasons": sorted({r["reason"] for r in d["redactions"]})} for d in hits]
        return {**doc, "decisions": decisions, "earlier": earlier,
                "redactions": [r for d in decisions.values() for r in d["redactions"]]}

    def docs(self) -> list[dict]:
        scores = self.scores()
        out = []
        for p in self.files():
            text = p.read_text(encoding="utf-8", errors="replace")
            sha = audit.sha256_file(p)
            events = self.events.for_file(p.name, sha)
            other = sum(1 for e in map(json.loads, self.events.lines()) if e["file"] == p.name and e["sha256"] != sha)
            doc = self.resolve(text, events)
            for el in doc["elements"]:
                el["score"] = scores.get((p.name, sha, json.dumps(el["parts"])))
            out.append({"name": p.name, "sha256": sha, "text": text, **doc, "stale": other})
        return out

    def scores(self) -> dict:
        """Newest model score per (file, sha256, element span)."""
        path = self.folder / SCORES_NAME
        out = {}
        if path.exists():
            for ln in path.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    r = json.loads(ln)
                    out[(r["file"], r["sha256"], json.dumps(r["span"]))] = {
                        k: r.get(k) for k in ("score", "description", "evidence", "special")}
        return out

    def act(self, body: dict) -> dict:
        path = self.file(body["file"])
        sha = audit.sha256_file(path)
        if body.get("sha256") != sha:
            raise ValueError("the file has changed since the page loaded; reload")
        text = path.read_text(encoding="utf-8", errors="replace")
        doc = self.resolve(text, self.events.for_file(path.name, sha))
        event = {"file": path.name, "sha256": sha, "reviewer": self.reviewer, "action": body["action"]}
        if body["action"] in ("split", "unsplit"):
            at = int(body.get("at", -1))
            if body["action"] == "split":
                el = next((e for e in doc["elements"] if any(a < at < b for a, b in e["parts"])), None)
                if el is None or (text[at - 1].isalnum() and text[at].isalnum()):
                    raise ValueError("choose a point inside the text, between two words or sentences")
                event.update(at=at, who=(body.get("who") or "").strip() or None, element=el["id"])
            else:
                if not any(e.get("split") and e["parts"][0][0] == at for e in doc["elements"]):
                    raise ValueError("no split here")
                event["at"] = at
            return self.events.append(event)
        element = next((e for e in doc["elements"] if e["id"] == body.get("element")), None)
        if element is None:
            raise ValueError("unknown element")
        event.update(element=element["id"], span=element["parts"])
        if body["action"] == "reset":
            return self.events.append(event)
        if body["action"] != "decide":
            raise ValueError("unknown action")
        decision = body.get("decision")
        if decision == "release":
            ranges = []
        elif decision == "redact_full":
            ranges = [{"start": a, "end": b, "reason": body.get("reason"), "note": body.get("note") or ""}
                      for a, b in element["parts"]]
        elif decision == "redact_partial":
            ranges = body.get("redactions") or []
            if not ranges:
                raise ValueError("mark at least one part to redact")
        else:
            raise ValueError("unknown decision")
        redactions = []
        for r in ranges:
            start, end = int(r["start"]), int(r["end"])
            if not any(a <= start < end <= b for a, b in element["parts"]):
                raise ValueError("a redaction lies outside this element")
            if r.get("reason") not in REASONS:
                raise ValueError("choose a reason for each redaction")
            note = (r.get("note") or "").strip()
            if r["reason"] == REASONS[-1] and not note:
                raise ValueError("'Other' needs a note")
            redactions.append({"id": secrets.token_hex(6), "start": start, "end": end, "reason": r["reason"],
                               "note": note, "reviewer": self.reviewer, "ts": _now(), "sha256": sha})
        event.update(decision=decision, redactions=redactions)
        return self.events.append(event)

    def export(self, name: str) -> tuple[str, Path]:
        doc = next(d for d in self.docs() if d["name"] == name)
        page = render_export(doc, self.reviewer)
        out_dir = self.folder / EXPORT_DIR
        out_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"{Path(name).stem}_REDACTED_{stamp}.html"
        path.write_text(page, encoding="utf-8")
        path.chmod(0o444)
        if self.case_output:
            decided, total = len(doc["decisions"]), len(doc["elements"])
            audit.AuditLog(self.case_output).append("markup_export", "markup export", {
                "file": name, "file_sha256": doc["sha256"], "export": str(path), "export_sha256": audit.sha256_file(path),
                "redactions": len(doc["redactions"]), "decided": f"{decided}/{total}",
                "decisions": dict(Counter(d["decision"] for d in doc["decisions"].values())), "reviewer": self.reviewer,
                "events_sha256": audit.sha256_file(self.events.path) if self.events.path.exists() else None})
        return page, path

    def export_extracts(self) -> dict:
        """Write the report text and the private extract register for the whole folder."""
        docs = self.docs()
        items = extracts(docs)
        undecided = {d["name"]: len(d["elements"]) - len(d["decisions"]) for d in docs
                     if len(d["decisions"]) < len(d["elements"])}
        out_dir = self.folder / EXTRACT_DIR
        out_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ("_DRAFT" if undecided else "")
        report = out_dir / f"personal_data_extracts_{stamp}.txt"
        report.write_text("\n\n".join(i["text"] for i in items) + "\n", encoding="utf-8")
        register = out_dir / f"extract_register_{stamp}.csv"
        with register.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["extract_no", "extract", "source_file", "file_sha256", "element", "speaker", "time",
                        "decision", "reviewer", "decided_utc"])
            for n, item in enumerate(items, start=1):
                for src in item["sources"]:
                    w.writerow([n, item["text"], src["file"], src["file_sha256"], src["element"], src["speaker"],
                                src["time"], src["decision"], src["reviewer"], src["decided"]])
        for p in (report, register):
            p.chmod(0o444)
        result = {"extracts": len(items), "sources": sum(len(i["sources"]) for i in items),
                  "merged_duplicates": sum(len(i["sources"]) - 1 for i in items), "undecided": undecided,
                  "report": str(report), "report_sha256": audit.sha256_file(report),
                  "register": str(register), "register_sha256": audit.sha256_file(register)}
        if self.case_output:
            audit.AuditLog(self.case_output).append("markup_extracts", "markup extracts", {
                **result, "reviewer": self.reviewer,
                "events_sha256": audit.sha256_file(self.events.path) if self.events.path.exists() else None})
        return result


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def extracts(docs: list[dict]) -> list[dict]:
    """Released text of every decided element, one extract per released passage, identical ones merged.

    Undecided elements are left out. Order is the order first seen (file, then element).
    """
    merged: dict[str, dict] = {}
    for d in docs:
        for el in d["elements"]:
            dec = d["decisions"].get(el["id"])
            if not dec:
                continue
            for a, b in el["parts"]:
                for piece, r in redacted_text(d["text"], a, b, dec["redactions"]):
                    text = " ".join(piece.split())
                    key = _norm(text)
                    if r is not None or not key:
                        continue
                    item = merged.setdefault(key, {"text": text, "sources": []})
                    item["sources"].append({"file": d["name"], "file_sha256": d["sha256"], "element": el["id"],
                                            "speaker": el["who"] or el["label"], "time": el["t"] or "",
                                            "decision": dec["decision"], "reviewer": dec["reviewer"], "decided": dec["ts"]})
    return list(merged.values())


def render_export(doc: dict, reviewer: str) -> str:
    """Printable redacted copy. Redacted text is never written into the page."""
    text, reds = doc["text"], doc["redactions"]
    numbered = {r["id"]: i for i, r in enumerate(sorted(reds, key=lambda r: r["start"]), start=1)}
    e = html.escape

    def body(start: int, end: int) -> str:
        return "".join(e(t) if r is None else
                       f'<span class="r">[Redacted {numbered[r["id"]]}: {e(r["reason"])}]</span>'
                       for t, r in redacted_text(text, start, end, reds))

    parts = []
    for el in doc["elements"]:
        inner = " ".join(body(a, b).strip() for a, b in el["parts"])
        if el["who"]:
            label = e(el["who"]) + (" (split by reviewer)" if el.get("split") else "")
            parts.append(f'<div class="turn"><b>{label}</b> <span class="t">{e(el["t"] or "")}</span><div>{inner}</div></div>')
        else:
            parts.append(f'<div class="txt">{inner}</div>')
    decided, total = len(doc["decisions"]), len(doc["elements"])
    draft = "" if decided == total else (f'<p class="draft">DRAFT: {total - decided} of {total} element(s) '
                                         f'not yet reviewed.</p>')
    log = "".join(f'<tr><td>{numbered[r["id"]]}</td><td>{e(r["reason"])}</td><td>{e(r.get("note") or "")}</td>'
                  f'<td>{e(r["reviewer"])}</td><td>{e(r["ts"])}</td><td>{r["start"]}-{r["end"]}</td></tr>'
                  for r in sorted(reds, key=lambda r: r["start"]))
    return EXPORT_PAGE.format(name=e(doc["name"]), sha=doc["sha256"], when=_now(), reviewer=e(reviewer), draft=draft,
                              body="".join(parts), n=len(reds),
                              log=log or '<tr><td colspan="6">No redactions.</td></tr>')


EXPORT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{name} (redacted)</title>
<style>body{{font:12pt/1.5 Georgia,serif;margin:2cm;color:#000;background:#fff}}h1{{font-size:14pt}}
.meta{{font:9pt/1.4 -apple-system,sans-serif;color:#444;border-bottom:1px solid #999;padding-bottom:8px}}
.draft{{font:bold 11pt sans-serif;color:#a00;border:2px solid #a00;padding:6px}}
.r{{background:#000;color:#fff;font:bold 9pt sans-serif;padding:0 4px}}
.turn{{margin:10px 0;break-inside:avoid}}.t{{color:#555;font-size:9pt}}.txt{{white-space:pre-wrap;margin:0 0 12px}}
table{{border-collapse:collapse;font:9pt sans-serif;width:100%}}td,th{{border:1px solid #999;padding:3px 6px;text-align:left}}
h2{{font-size:12pt;break-before:page}}</style></head><body>
<h1>{name}: redacted copy</h1>
<div class="meta">Source SHA-256 {sha}<br>Exported {when} by {reviewer}. Redacted text has been removed from this copy.</div>
{draft}{body}
<h2>Redaction log ({n})</h2><table><tr><th>#</th><th>Reason</th><th>Note</th><th>By</th><th>When (UTC)</th><th>Characters</th></tr>{log}</table>
</body></html>"""


def element_text(text: str, el: dict) -> str:
    return " ".join(text[a:b].strip() for a, b in el["parts"])


async def score_folder(folder: Path, *, system_prompt: str, model: str, subject_name: str, max_tokens: int,
                       temperature: float, concurrency: int, progress=None) -> dict:
    """Score every element not yet scored with this model and prompt; append to SCORES_NAME."""
    import asyncio

    import httpx

    from dsar import classify

    f = Folder(folder, "", None)
    prompt_sha = hashlib.sha256(system_prompt.encode()).hexdigest()
    path = Path(folder) / SCORES_NAME
    done = set()
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                if r.get("model") == model and r.get("prompt_sha256") == prompt_sha and r.get("score") is not None:
                    done.add((r["file"], r["sha256"], json.dumps(r["span"])))
    todo = []
    for d in f.docs():
        for el in d["elements"]:
            if (d["name"], d["sha256"], json.dumps(el["parts"])) not in done and element_text(d["text"], el).strip():
                label = f"{el['who']} at {el['t']}" if el["who"] else el["label"]
                todo.append((d["name"], d["sha256"], el["parts"], label, element_text(d["text"], el)))
    sem, lock, counts = asyncio.Semaphore(max(1, concurrency)), threading.Lock(), Counter()

    async def one(client, item):
        name, sha, span, label, text = item
        user = classify.build_user_prompt("document passage", f"{name}, {label}", subject_name, text)
        async with sem:
            try:
                resp = await client.post("/api/chat", json=classify.chat_request(
                    model, system_prompt, [{"role": "user", "content": user}],
                    max_tokens=max_tokens, temperature=temperature))
                resp.raise_for_status()
                parsed = classify.parse_response(resp.json()["message"]["content"])
            except Exception as exc:                               # noqa: BLE001 - recorded, run continues
                counts["failed"] += 1
                parsed = classify.ParsedResponse(None, f"error: {exc}"[:200], None, None, "")
        row = {"file": name, "sha256": sha, "span": span, "score": parsed.score, "description": parsed.description,
               "evidence": parsed.evidence, "special": parsed.special_return, "model": model,
               "prompt_sha256": prompt_sha, "ts": _now()}
        with lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts["scored"] += parsed.score is not None
            counts["done"] += 1
            if progress:
                progress(counts["done"], len(todo))

    async with httpx.AsyncClient(base_url=classify.ollama_url(), timeout=300.0) as client:
        await asyncio.gather(*(one(client, item) for item in todo))
    return {"elements": len(todo), "already_scored": len(done), "scored": counts["scored"], "failed": counts["failed"],
            "prompt_sha256": prompt_sha, "model": model}


class _Handler(BaseHTTPRequestHandler):
    folder: Folder
    token: str

    def log_message(self, *args) -> None:       # keep request lines (and file names) out of the terminal
        pass

    def _send(self, code: int, body: str | bytes, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                                                    "script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self, query: dict) -> bool:
        return secrets.compare_digest(query.get("token", [""])[0] or self.headers.get("X-Token", ""), self.token)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self._authorised(q):
            return self._send(403, '{"error": "missing or wrong token"}')
        if url.path == "/":
            return self._send(200, APP_PAGE.replace("__REASONS__", json.dumps(REASONS)), "text/html")
        if url.path == "/api/docs":
            return self._send(200, json.dumps({"reviewer": self.folder.reviewer, "docs": self.folder.docs(),
                                               "chain_ok": self.folder.events.verify()}))
        if url.path == "/api/extracts":
            return self._send(200, json.dumps(self.folder.export_extracts()))
        if url.path == "/export":
            try:
                page, path = self.folder.export(q["file"][0])
            except (KeyError, StopIteration):
                return self._send(404, '{"error": "no such file"}')
            return self._send(200, page, "text/html")
        self._send(404, '{"error": "not found"}')

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/api/act" or not self._authorised({}):
            return self._send(403, '{"error": "forbidden"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            entry = self.folder.act(body)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        self._send(200, json.dumps(entry))


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host's FQDN, which can stall for 30s+ on some networks.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def serve(folder: Path, reviewer: str, case_output: Path | None, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the server on 127.0.0.1 and return it with the URL to open (it carries a one-off token)."""
    token = secrets.token_urlsafe(24)
    handler = type("Handler", (_Handler,), {"folder": Folder(folder, reviewer, case_output), "token": token})
    server = _LocalServer(("127.0.0.1", port), handler)
    return server, f"http://127.0.0.1:{server.server_address[1]}/?token={token}"




APP_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Redaction markup</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#2f5d8a;--subj:#fff4d6;--subj-line:#e0a800;--red:#b3261e;--redbg:#fbe3e1;--ok:#2e7d4f;--okbg:#e6f4ea;--part:#8a5a00;--partbg:#fdf0d5}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe6;--muted:#9a9993;--line:#34332f;--accent:#8db4dc;--subj:#3a3220;--red:#f08a7e;--redbg:#432421;--ok:#6cc08b;--okbg:#1d3324;--part:#f0c060;--partbg:#3a2f15}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.app{display:grid;grid-template-columns:300px 1fr;min-height:100vh}
nav{border-right:1px solid var(--line);background:var(--panel);padding:16px;position:sticky;top:0;height:100vh;overflow:auto}
nav .who{font-size:12px;color:var(--muted);margin-bottom:12px}
nav button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ink);padding:8px 10px;border-radius:6px;font:inherit;font-size:14px;cursor:pointer}
nav button:hover{background:var(--bg)}nav button.on{background:var(--accent);color:#fff}nav .prog{display:block;font-size:12px;opacity:.8}
main{padding:0 32px 64px;max-width:940px;width:100%}
.bar{position:sticky;top:0;background:var(--bg);padding:12px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line);z-index:3}
.bar input[type=search]{flex:1;min-width:160px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:4px 10px;font:inherit;font-size:13px;cursor:pointer}
.btn:hover{border-color:var(--ink)}.btn.go{background:var(--ok);border-color:var(--ok);color:#fff}.btn.stop{background:var(--red);border-color:var(--red);color:#fff}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.toggle{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}.toggle button{border:0;background:var(--panel);color:var(--ink);padding:6px 12px;font:inherit;font-size:14px;cursor:pointer}.toggle button.on{background:var(--ink);color:var(--bg)}
.legend{font-size:13px;color:var(--muted);margin:12px 0;display:flex;gap:14px;flex-wrap:wrap}.legend span{display:inline-flex;gap:6px;align-items:center}.sw{width:14px;height:14px;border-radius:3px;display:inline-block}
.warn{background:var(--redbg);border:1px solid var(--red);border-radius:8px;padding:8px 12px;margin:12px 0;font-size:14px}
.el{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--line);border-radius:8px;padding:10px 14px;margin:10px 0}
.el.subj{background:var(--subj)}
.el.release{border-left-color:var(--ok)}.el.redact_full{border-left-color:var(--red)}.el.redact_partial{border-left-color:var(--part)}.el.editing{box-shadow:0 0 0 2px var(--accent)}
.el .h{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:14px}.el .h b{color:var(--c)}.el .t{color:var(--muted);font-size:12px}.el .sp{flex:1}
.badge{font-size:12px;font-weight:600;padding:2px 10px;border-radius:99px}
.badge.release{background:var(--okbg);color:var(--ok)}.badge.redact_full{background:var(--redbg);color:var(--red)}.badge.redact_partial{background:var(--partbg);color:var(--part)}.badge.pending{background:var(--bg);color:var(--muted)}
.txt{white-space:pre-wrap;word-wrap:break-word;margin-top:6px}
.rel{background:var(--okbg);border-radius:2px}
.cut{background:var(--redbg);color:var(--red);text-decoration:line-through;border-radius:2px}
.why{font-size:11px;font-weight:600;color:var(--red);text-decoration:none;margin:0 4px;white-space:nowrap}
.mark{background:var(--redbg);color:var(--red);text-decoration:line-through;cursor:pointer;border-radius:2px;outline:1px dashed var(--red)}
.blk{background:#000;color:#fff;font-size:12px;padding:1px 6px;border-radius:2px;white-space:nowrap}
.form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px;padding:8px;background:var(--bg);border-radius:6px;font-size:13px}
.form select,.form input{padding:4px 6px;font:inherit;font-size:13px;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px}.form input{flex:1;min-width:140px}
mark{background:#ffe066;color:#000}
.prior{font-size:13px;background:var(--partbg);color:var(--part);border-radius:6px;padding:4px 10px;margin-top:6px}
.cutpt{display:inline-block;width:3px;height:1.2em;background:var(--accent);vertical-align:text-bottom;margin:0 1px}
.el.splitting .txt{cursor:crosshair}
.sc{font-size:12px;font-weight:700;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted);cursor:help}
.sc.hi{background:var(--redbg);color:var(--red);border-color:var(--red)}.sc.mid{background:var(--partbg);color:var(--part);border-color:var(--part)}.sc.lo{background:var(--bg)}
.el.focus{outline:2px solid var(--accent);outline-offset:2px}
.btn.big{font-size:14px;padding:6px 14px;font-weight:600}
details.more{position:relative}details.more summary{list-style:none;cursor:pointer;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);font-size:13px}details.more summary::-webkit-details-marker{display:none}
details.more .menu{position:absolute;right:0;top:110%;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.15);padding:4px;z-index:5;min-width:150px}
details.more .menu button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ink);padding:6px 10px;font:inherit;font-size:13px;border-radius:4px;cursor:pointer}details.more .menu button:hover{background:var(--bg)}
.keys{font-size:12px;color:var(--muted)}.keys kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 5px;background:var(--panel);font:inherit;font-size:11px}
.done{margin:12px 0;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:2px 8px}
.done .el{margin:8px 0}
.dbar{display:flex;gap:10px;width:100%;align-items:center;border:0;background:none;color:var(--ink);font:inherit;font-size:14px;font-weight:600;padding:8px 4px;cursor:pointer;text-align:left}.dbar .sp{flex:1}
.row{display:flex;gap:8px;align-items:center;padding:4px 6px;border-top:1px solid var(--line);font-size:13px;cursor:pointer}.row:hover{background:var(--bg)}
.row b{white-space:nowrap}.row .snip{color:var(--muted);overflow:hidden;white-space:nowrap;text-overflow:ellipsis;flex:1;min-width:0}
.alldone{background:var(--okbg);color:var(--ok);border-radius:8px;padding:10px 14px}
.toast{position:fixed;bottom:16px;right:16px;background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:6px;font-size:14px;display:none}
@media (max-width:760px){.app{grid-template-columns:1fr}nav{position:static;height:auto}main{padding:0 16px 48px}}
</style></head><body>
<div class="app"><nav><div class="who" id="who"></div><div id="files"></div><button class="btn primary" id="ext" style="margin-top:16px;width:100%">Export Personal Data extracts</button><div id="extres" class="keys" style="margin-top:8px"></div></nav><main id="main"></main></div><div class="toast" id="toast"></div>
<script>
const TOKEN=new URLSearchParams(location.search).get("token")||(document.querySelector('meta[name="csrf"]')||{}).content,REASONS=__REASONS__,SUBJECT=(new URLSearchParams(location.search).get("subject")||"").toLowerCase();
const COLOURS=["#2f5d8a","#2e7d4f","#8a4b9c","#b5651d","#1f7a8c"];
const LABEL={release:"Released",redact_full:"Redacted in full",redact_partial:"Partly redacted",pending:"Not reviewed"};
let DOCS=[],cur=0,view="original",q="",edit=null,full=null,split=null,minScore=-1,focus=null,lastReason=REASONS[0],doneOpen=false,openRow=null;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const reasonOpts=()=>REASONS.map(x=>`<option>${esc(x)}</option>`).join("");
function toast(m){const t=document.getElementById("toast");t.textContent=m;t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",3000)}
async function load(keep){const j=await (await fetch("api/docs?token="+TOKEN)).json();DOCS=j.docs;document.getElementById("who").innerHTML="Reviewer: <b>"+esc(j.reviewer)+"</b>"+(j.chain_ok?"":" · <span style='color:var(--red)'>event log chain broken</span>");if(!keep)cur=0;nav();render();if(!focus){const d=DOCS[cur];focus=(d.elements.find(e=>!d.decisions[e.id])||{}).id;render()}}
async function act(body){Object.assign(body,{file:DOCS[cur].name,sha256:DOCS[cur].sha256});const r=await fetch("api/act",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify(body)});const j=await r.json();if(!r.ok){toast(j.error);return false}return true}
function nav(){const f=document.getElementById("files");f.innerHTML="";DOCS.forEach((d,i)=>{const n=Object.keys(d.decisions).length,b=document.createElement("button");b.className=i===cur?"on":"";b.innerHTML=esc(d.name.replace(/\.txt$/,"").replace(/_/g," "))+`<span class="prog">${n}/${d.elements.length} reviewed · ${d.redactions.length} redaction(s)</span>`;b.onclick=()=>{if(edit&&!confirm("Discard unsaved marks?"))return;cur=i;q="";edit=null;full=null;split=null;focus=(d.elements.find(e=>!d.decisions[e.id])||{}).id;nav();render();scrollTo(0,0)};f.appendChild(b)})}
function hl(s){let e=esc(s);if(q){const re=new RegExp("("+q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","gi");e=e.replace(re,"<mark>$1</mark>")}return e}
// One part [a,b] of an element, with redactions rs; cls is the class for released text
function piece(d,a,b,rs,cls,editable){let out="",pos=a;const seg0=(x,y)=>`<span data-s="${x}" class="${cls}">${hl(d.text.slice(x,y))}</span>`;
 const seg=(x,y)=>split&&split.at>x&&split.at<y?seg0(x,split.at)+`<span class="cutpt"></span>`+seg0(split.at,y):seg0(x,y);
 for(const r of rs.filter(r=>r.end>a&&r.start<b).sort((x,y)=>x.start-y.start)){const s=Math.max(r.start,a),e=Math.min(r.end,b);if(s<pos)continue;if(s>pos)out+=seg(pos,s);
  if(editable)out+=`<span class="mark" data-s="${s}" data-i="${r.i}" title="${esc(r.reason)} (click to unmark)">${hl(d.text.slice(s,e))}</span>`;
  else if(view==="redacted")out+=`<span class="blk">█ Redacted: ${esc(r.reason)}</span>`;
  else out+=`<span class="cut" data-s="${s}" title="${esc(r.reason)}${r.note?": "+esc(r.note):""}">${hl(d.text.slice(s,e))}</span><span class="why">[${esc(r.reason)}]</span>`;pos=e}
 if(pos<b)out+=seg(pos,b);return out}
// One line for a processed element; click it to open the full element.
function row(d,el){const dec=d.decisions[el.id],st=dec.decision,text=el.parts.map(([a,b])=>d.text.slice(a,b)).join(" ").replace(/\s+/g," ").trim();
 return `<div class="row" data-row="${el.id}" title="Click to review or change"><b>${esc(el.who||el.label)}</b><span class="t">${esc(el.t||"")}</span><span class="badge ${st}">${LABEL[st]}${st==="redact_partial"?": "+dec.redactions.length:""}</span><span class="snip">${esc(text.slice(0,140))}</span></div>`}
// The three decision buttons, on every element; the current decision is shown pressed.
function buttons(st){return `<button class="btn big ${st==="release"?"go":""}" data-a="approve" title="A">${st==="release"?"✓ ":""}Approve</button><button class="btn big ${st==="redact_full"?"stop":""}" data-a="full" title="R">${st==="redact_full"?"✓ ":""}Redact all</button><button class="btn big ${st==="redact_partial"?"primary":""}" data-a="partial" title="P">${st==="redact_partial"?"✓ Edit parts":"Redact part"}</button>`}
function more(el,decided){return `<details class="more"><summary title="More">⋯</summary><div class="menu"><button data-a="split">Split…</button>${el.split?'<button data-a="unsplit">Undo split</button>':""}${decided?'<button data-a="change">Clear decision</button>':""}</div></details>`}
function render(){const d=DOCS[cur],m=document.getElementById("main");if(!d){m.innerHTML="<p>No .txt files in this folder.</p>";return}
 const who=[...new Set(d.elements.map(e=>e.who).filter(Boolean))],n=Object.keys(d.decisions).length;
 let h=`<div class="bar"><input type="search" id="q" placeholder="Search…" value="${esc(q)}"><span class="toggle"><button data-v="original" class="${view==="original"?"on":""}">Original</button><button data-v="redacted" class="${view==="redacted"?"on":""}">Redacted</button></span>
  <select id="min" class="btn" title="Show only elements the model scored at or above this"><option value="-1">All scores</option>${[1,5,6,7,8].map(n=>`<option value="${n}" ${minScore===n?"selected":""}>Model score ≥ ${n}</option>`).join("")}</select><button class="btn" id="all">Approve all not yet reviewed</button><button class="btn primary" id="exp">Export redacted copy</button></div>
  <div class="legend"><span><i class="sw" style="background:var(--okbg);border:1px solid var(--ok)"></i>Released</span><span><i class="sw" style="background:var(--redbg);border:1px solid var(--red)"></i>Redacted</span><span>${n} of ${d.elements.length} elements reviewed</span><span class="keys"><kbd>A</kbd> approve · <kbd>R</kbd> redact all · <kbd>P</kbd> redact part · <kbd>↓</kbd><kbd>↑</kbd> move · <kbd>Enter</kbd> confirm</span></div>`;
 if(d.stale)h+=`<div class="warn">${d.stale} earlier decision(s) were made on a different version of this file and are not shown.</div>`;
 const cards={},shown=[];
 for(const el of d.elements){const text=el.parts.map(([a,b])=>d.text.slice(a,b)).join(" ");
  if(q&&!text.toLowerCase().includes(q.toLowerCase())&&!(el.who||"").toLowerCase().includes(q.toLowerCase())&&edit?.el!==el.id)continue;
  if(minScore>=0&&!(el.score&&el.score.score>=minScore)&&edit?.el!==el.id&&split?.el!==el.id)continue;
  shown.push(el);
  const dec=d.decisions[el.id],st=dec?dec.decision:"pending",editing=edit&&edit.el===el.id,c=el.who?COLOURS[who.indexOf(el.who)%COLOURS.length]:"var(--muted)";
  const subj=el.who&&SUBJECT&&el.who.toLowerCase().startsWith(SUBJECT);
  const splitting=split&&split.el===el.id;let body,ctl;
  if(editing){const rs=edit.marks.map((r,i)=>({...r,i}));body=el.parts.map(([a,b])=>piece(d,a,b,rs,"",true)).join(" ");
   ctl=`<span class="badge redact_partial">Marking parts: ${edit.marks.length}</span><span class="sp"></span><button class="btn go" data-a="save">Save</button><button class="btn" data-a="cancel">Cancel</button>`}
  else if(dec){body=el.parts.map(([a,b])=>piece(d,a,b,dec.redactions,st==="release"||st==="redact_partial"?"rel":"",false)).join(" ");
   if(st==="redact_full"&&view==="redacted")body=`<span class="blk">█ Redacted in full: ${esc(dec.redactions[0]?.reason||"")}</span>`;
   ctl=`<span class="badge ${st}">${LABEL[st]}${st==="redact_partial"?": "+dec.redactions.length+" part(s)":""}${st==="redact_full"?": "+esc(dec.redactions[0]?.reason||""):""}</span><span class="t">${esc(dec.reviewer)}, ${esc(dec.ts.slice(0,16).replace("T"," "))}</span><span class="sp"></span>${buttons(st)}${more(el,true)}`}
  else{body=el.parts.map(([a,b])=>piece(d,a,b,[],"",false)).join(" ");
   ctl=`<span class="badge pending">Not reviewed</span><span class="sp"></span>${buttons(st)}${more(el,false)}`}
  const prior=(d.earlier[el.id]||[]).map(p=>`${LABEL[p.decision]}${p.reasons.length?" ("+esc(p.reasons.join(", "))+")":""}, ${esc(p.reviewer)} ${esc(p.ts.slice(0,16).replace("T"," "))}`).join("; ");
  let form="";
  if(editing)form=`<div class="form">Select text above to mark it. Reason for new marks: <select id="er">${reasonOpts()}</select><input id="en" placeholder="Note (required for Other)" value="${esc(edit.note||"")}"></div>`;
  if(splitting){const opts=[...who,"Unknown speaker"].map(w=>`<option ${w===split.who?"selected":""}>${esc(w)}</option>`).join("");
   form=`<div class="form">${split.at==null?"Click in the text where the new part should start.":"New part starts at the blue line. Speaker of the new part:"} <select id="sw">${opts}</select><button class="btn primary" data-a="splitok" ${split.at==null?"disabled":""}>Split here</button><button class="btn" data-a="splitcancel">Cancel</button></div>`}
  if(full===el.id)form=`<div class="form">Redact all of this element. Reason: <select id="fr">${REASONS.map(x=>`<option ${x===lastReason?"selected":""}>${esc(x)}</option>`).join("")}</select><input id="fn" placeholder="Note (required for Other)"><button class="btn stop" data-a="fullok">Redact all (Enter)</button><button class="btn" data-a="fullcancel">Cancel (Esc)</button></div>`;
  const s=el.score,chip=s?(s.score==null?`<span class="sc lo" title="${esc(s.special||s.description||"")}">Model: –</span>`:`<span class="sc ${s.score>=7?"hi":s.score>=5?"mid":"lo"}" title="Model suggestion, not a decision: ${esc(s.description||"")}${s.evidence?" | "+esc(s.evidence):""}">Model ${s.score}</span>`):"";
  const name=el.who?`<b>${esc(el.who)}${el.split?" <span class='t'>(split by reviewer)</span>":""}</b><span class="t">${esc(el.t||"")}</span>`:`<b style="color:var(--muted)">${esc(el.label)}</b>`;
  cards[el.id]=`<div class="el ${st} ${subj?"subj":""} ${editing?"editing":""} ${splitting?"editing splitting":""} ${focus===el.id?"focus":""}" data-el="${el.id}" style="--c:${c}"><div class="h">${name}${chip}${ctl}</div>${prior?`<div class="prior">Decided before this element was split: ${prior}. Please decide again.</div>`:""}<div class="txt">${body}</div>${form}</div>`}
 // Processed elements collapse into one bar at the top; the rest follow in document order.
 const active=id=>edit?.el===id||full===id||split?.el===id||openRow===id;
 const done=shown.filter(el=>d.decisions[el.id]),todo=shown.filter(el=>!d.decisions[el.id]);
 if(done.length){const cnt={};done.forEach(el=>{const k=d.decisions[el.id].decision;cnt[k]=(cnt[k]||0)+1});
  h+=`<div class="done"><button class="dbar" id="dtoggle">${doneOpen?"▾":"▸"} Processed: ${done.length}<span class="t">${Object.entries(cnt).map(([k,v])=>v+" "+LABEL[k].toLowerCase()).join(" · ")}</span><span class="sp"></span><span class="t">${doneOpen?"Hide":"Show to review or change"}</span></button>`;
  h+=doneOpen?done.map(el=>active(el.id)?cards[el.id]:row(d,el)).join(""):done.filter(el=>active(el.id)).map(el=>cards[el.id]).join("");
  h+=`</div>`}
 h+=todo.length?todo.map(el=>cards[el.id]).join(""):`<p class="alldone">Every element ${q||minScore>=0?"shown here ":""}is reviewed. Open “Processed” to check them, or export the redacted copy.</p>`;
 m.innerHTML=h;
 if(edit){const s=document.getElementById("er");if(s)s.value=edit.reason||REASONS[0]}
 const qi=document.getElementById("q");qi.oninput=()=>{q=qi.value;const p=qi.selectionStart;render();const n=document.getElementById("q");n.focus();n.setSelectionRange(p,p)};
 m.querySelectorAll(".toggle button").forEach(b=>b.onclick=()=>{view=b.dataset.v;render()});
 const dt=document.getElementById("dtoggle");if(dt)dt.onclick=()=>{doneOpen=!doneOpen;openRow=null;render()};
 m.querySelectorAll(".row").forEach(r=>r.onclick=()=>{openRow=r.dataset.row;focus=openRow;render()});
 document.getElementById("min").onchange=ev=>{minScore=+ev.target.value;render()};
 document.getElementById("exp").onclick=()=>window.open("export?token="+TOKEN+"&file="+encodeURIComponent(d.name),"_blank");
 document.getElementById("all").onclick=async()=>{const todo=d.elements.filter(e=>!d.decisions[e.id]);if(!todo.length||!confirm(`Approve ${todo.length} element(s) not yet reviewed, for release as they are?`))return;for(const e of todo)if(!await act({action:"decide",element:e.id,decision:"release"}))break;await load(true)};
 m.querySelectorAll("[data-a]").forEach(b=>b.onclick=async()=>{const id=b.closest(".el").dataset.el,a=b.dataset.a;
  if(a==="approve"){if(DOCS[cur].decisions[id]?.decision==="release")return advance(id);if(await act({action:"decide",element:id,decision:"release"})){await load(true);advance(id)}}
  else if(a==="full"){if(edit)return toast("Save or cancel the element you are marking first");focus=id;full=id;render()}
  else if(a==="fullcancel"){full=null;render()}
  else if(a==="fullok"){lastReason=document.getElementById("fr").value;if(await act({action:"decide",element:id,decision:"redact_full",reason:lastReason,note:document.getElementById("fn").value})){full=null;await load(true);advance(id)}}
  else if(a==="partial"){if(edit)return toast("Save or cancel the element you are marking first");const prev=DOCS[cur].decisions[id];view="original";full=null;split=null;
   edit={el:id,marks:prev&&prev.decision==="redact_partial"?prev.redactions.map(r=>({start:r.start,end:r.end,reason:r.reason,note:r.note})):[],reason:REASONS[0]};render()}
  else if(a==="cancel"){edit=null;render()}
  else if(a==="save"){if(await act({action:"decide",element:id,decision:"redact_partial",redactions:edit.marks})){edit=null;await load(true);advance(id)}}
  else if(a==="split"){if(edit)return toast("Save or cancel the element you are marking first");full=null;split={el:id,at:null,who:"Unknown speaker"};render()}
  else if(a==="splitcancel"){split=null;render()}
  else if(a==="splitok"){const w=document.getElementById("sw").value;if(await act({action:"split",at:split.at,who:w==="Unknown speaker"?"Unknown speaker":w})){split=null;await load(true)}}
  else if(a==="unsplit"){if(confirm("Undo this split? Decisions on either part will need to be made again."))if(await act({action:"unsplit",at:+id.slice(1)}))await load(true)}
  else if(a==="change"){if(confirm("Clear this element's decision? It becomes not reviewed (the earlier decision stays in the log)."))if(await act({action:"reset",element:id}))await load(true)}});
 m.querySelectorAll(".el").forEach(x=>x.addEventListener("mousedown",ev=>{if(!ev.target.closest("button,select,input,summary"))focus=x.dataset.el;m.querySelectorAll(".el.focus").forEach(y=>y.classList.remove("focus"));x.classList.add("focus")}));
 const fr=document.getElementById("fr");if(fr){fr.focus();fr.onkeydown=ev=>{if(ev.key==="Enter"){ev.preventDefault();m.querySelector('[data-a="fullok"]').click()}}}
 m.querySelectorAll(".mark").forEach(s=>s.onclick=()=>{edit.marks.splice(+s.dataset.i,1);render()});
 const er=document.getElementById("er"),en=document.getElementById("en");if(er)er.onchange=()=>edit.reason=er.value;if(en)en.oninput=()=>edit.note=en.value;
}
function visibleIds(){return [...document.querySelectorAll("#main .el")].map(x=>x.dataset.el)}
function goTo(id){if(!id)return;focus=id;document.querySelectorAll(".el.focus").forEach(y=>y.classList.remove("focus"));const x=document.querySelector(`.el[data-el="${id}"]`);if(x){x.classList.add("focus");x.scrollIntoView({block:"center",behavior:"smooth"})}}
// After a decision, move to the next element not yet reviewed (in the current view and filter).
function advance(id){openRow=null;render();const d=DOCS[cur],ids=visibleIds(),i=ids.indexOf(id);const next=ids.slice(i+1).find(x=>!d.decisions[x])||ids.find(x=>!d.decisions[x]);if(next)goTo(next);else toast("Every element shown here is reviewed")}
document.addEventListener("keydown",ev=>{if(ev.metaKey||ev.ctrlKey||ev.altKey)return;const t=ev.target;
 if(ev.key==="Escape"){if(full){full=null;render()}else if(split){split=null;render()}else if(edit&&confirm("Discard unsaved marks?")){edit=null;render()}return}
 if(t.closest&&t.closest("input,select,textarea"))return;if(edit||split||full)return;
 const ids=visibleIds();if(!ids.length)return;let i=ids.indexOf(focus);
 const k=ev.key.toLowerCase();
 if(k==="arrowdown"||k==="j"){ev.preventDefault();goTo(ids[Math.min(i+1,ids.length-1)]||ids[0])}
 else if(k==="arrowup"||k==="k"){ev.preventDefault();goTo(ids[Math.max(i-1,0)])}
 else if(["a","r","p"].includes(k)){if(i<0){goTo(ids.find(x=>!DOCS[cur].decisions[x])||ids[0]);return}ev.preventDefault();
  const b=document.querySelector(`.el[data-el="${focus}"] [data-a="${{a:"approve",r:"full",p:"partial"}[k]}"]`);if(b)b.click()}});
function offset(node,off){const el=(node.nodeType===3?node.parentElement:node).closest("[data-s]");if(!el)return null;let n=0;const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let t;while((t=w.nextNode())){if(t===node)return +el.dataset.s+n+off;n+=t.textContent.length}return +el.dataset.s+(node.nodeType===3?off:n)}
document.addEventListener("click",ev=>{if(!split)return;const box=document.querySelector(`.el[data-el="${split.el}"] .txt`);if(!box||!box.contains(ev.target))return;
 let node,off;if(document.caretRangeFromPoint){const r=document.caretRangeFromPoint(ev.clientX,ev.clientY);if(!r)return;node=r.startContainer;off=r.startOffset}else if(document.caretPositionFromPoint){const p=document.caretPositionFromPoint(ev.clientX,ev.clientY);if(!p)return;node=p.offsetNode;off=p.offset}else return;
 const at=offset(node,off);if(at==null)return;const sw=document.getElementById("sw");split.who=sw?sw.value:split.who;split.at=at;render()});
document.addEventListener("mouseup",()=>{if(!edit)return;const sel=getSelection();if(!sel.rangeCount||sel.isCollapsed)return;const r=sel.getRangeAt(0);
 const box=document.querySelector(`.el[data-el="${edit.el}"] .txt`);if(!box||!box.contains(r.startContainer)||!box.contains(r.endContainer)){toast("Select text inside the element you are marking");return}
 let a=offset(r.startContainer,r.startOffset),b=offset(r.endContainer,r.endOffset);if(a==null||b==null)return;if(b<a)[a,b]=[b,a];
 const d=DOCS[cur],el=d.elements.find(e=>e.id===edit.el),reason=document.getElementById("er").value,note=document.getElementById("en").value;
 if(reason===REASONS[REASONS.length-1]&&!note.trim()){toast("'Other' needs a note");return}
 for(const [pa,pb] of el.parts){let s=Math.max(a,pa),e=Math.min(b,pb);while(s<e&&/\s/.test(d.text[s]))s++;while(e>s&&/\s/.test(d.text[e-1]))e--;if(e<=s)continue;
  const keep=[];for(const k of edit.marks){if(k.end<s||k.start>e)keep.push(k);else{s=Math.min(s,k.start);e=Math.max(e,k.end)}}keep.push({start:s,end:e,reason,note});edit.marks=keep}
 sel.removeAllRanges();render()});
document.getElementById("ext").onclick=async()=>{const und=DOCS.filter(d=>Object.keys(d.decisions).length<d.elements.length);
 if(und.length&&!confirm(und.length+" file(s) still have elements not reviewed; those elements are left out and the export is marked DRAFT. Export anyway?"))return;
 const j=await (await fetch("api/extracts?token="+TOKEN)).json();
 document.getElementById("extres").innerHTML=`${j.extracts} extract(s) from ${j.sources} passage(s); ${j.merged_duplicates} duplicate(s) merged.<br>Saved to ${esc(j.report.split("/").slice(-2).join("/"))} and the register beside it.`;toast("Extracts exported")};
load();
</script></body></html>"""
