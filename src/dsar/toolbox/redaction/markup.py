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

"Export Personal Data extracts" collects every extracted element across the
folder into plain report text: one extract per element (the parts left out
removed, its lines kept), identical extracts merged, no headings or sources. A private register beside it lists every
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
    "Already reviewed and assessed",
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


SETTINGS_NAME = "review_settings.json"   # per folder: {"min_score": N}, the paragraph review threshold


class Folder:
    def __init__(self, folder: Path, reviewer: str, case_output: Path | None):
        self.folder = Path(folder)
        self.reviewer = reviewer
        self.case_output = case_output
        self.events = Events(self.folder)
        self.min_score = REVIEW_MIN_SCORE
        s = self.folder / SETTINGS_NAME
        if s.is_file():
            try:
                self.min_score = int(json.loads(s.read_text(encoding="utf-8")).get("min_score", REVIEW_MIN_SCORE))
            except (ValueError, TypeError):
                pass

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
                active.append({**{k: e[k] for k in ("decision", "redactions", "reviewer", "ts")}, "span": span,
                               "copied_from": e.get("copied_from", "")})
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
                el["blank"] = is_blank(text, el)
                el["below"] = below_threshold(el, self.min_score)
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
        if body.get("copied_from"):
            event["copied_from"] = str(body["copied_from"])[:300]     # "Apply the same decisions to repeats"
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
        undecided = {d["name"]: n for d in docs if (n := undecided_count(d))}
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


_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")
SCREEN_PLACEHOLDER = "\u25a1"   # □


def screen_text(text: str) -> str:
    """Text for the browser, one unit per character. Positions here are Python characters, but the
    browser counts an emoji (or other character beyond U+FFFF) as two, which shifted everything after
    it: text was cut short on screen and a cut could land two characters off. Each such character is
    shown as □ instead; the file and every position stay exactly as they are."""
    return _ASTRAL.sub(SCREEN_PLACEHOLDER, text)


def screen_docs(docs: list[dict]) -> list[dict]:
    return [{**d, "text": screen_text(d["text"])} for d in docs]


def is_blank(text: str, el: dict) -> bool:
    """No letters or digits (a '---' rule, a lone bullet): nothing to decide, nothing to extract."""
    return not re.search(r"\w", "".join(text[a:b] for a, b in el["parts"]))


REVIEW_MIN_SCORE = 7          # operator instruction 2026-11-17: only elements the model scores 7+ are reviewed


def below_threshold(el: dict, min_score: int = REVIEW_MIN_SCORE) -> bool:
    """Scored by the model, below the review threshold: needs no decision and stays out of the report.
    An element not yet scored (or the model gave no number) still needs a decision."""
    s = el.get("score") or {}
    return s.get("score") is not None and s["score"] < min_score


def section_counts(docs: list[dict], repeats: dict[str, dict]) -> dict[str, int]:
    """How many elements are in each Reviewer section (same rules as the page)."""
    c = {"review": 0, "low": 0, "repeats": 0, "done": 0}
    for d in docs:
        for el in d["elements"]:
            if el["id"] in d["decisions"]:
                c["done"] += 1
            elif el.get("blank"):
                continue
            elif el["id"] in repeats.get(d["name"], {}):
                c["repeats"] += 1
            elif el.get("below"):
                c["low"] += 1
            else:
                c["review"] += 1
    return c


def undecided_count(doc: dict) -> int:
    return sum(1 for el in doc["elements"]
               if el["id"] not in doc["decisions"] and not el.get("blank") and not el.get("below"))


REPEAT_MIN_CHARS = 20          # below this, a repeat must be exactly the same text (not just the same words)


def _element_raw(text: str, el: dict) -> str:
    return "\n".join(text[a:b] for a, b in el["parts"]).strip()


DUPLICATE = "Duplicate – already in the Personal Data Report"
_DUP_REASONS = {DUPLICATE, "Already reviewed and assessed"}


def decided_segments(text: str, el: dict, dec: dict) -> list[dict]:
    """The element as decided: each stretch extracted or left out (with its reason), for showing the earlier decision."""
    out = []
    for i, (a, b) in enumerate(el["parts"]):
        if i:
            out.append({"text": "\n", "out": False})
        if dec["decision"] == "redact_full":
            out.append({"text": screen_text(text[a:b]), "out": True,
                        "reason": dec["redactions"][0]["reason"] if dec["redactions"] else ""})
            continue
        for piece, r in redacted_text(text, a, b, dec["redactions"]):
            if r is None:
                out.append({"text": screen_text(piece), "out": False})
            else:
                out.append({"text": screen_text(text[max(r["start"], a):min(r["end"], b)]), "out": True, "reason": r["reason"]})
    return out


CONFLICT_MIN_CHARS = 25


def conflicts(folders: list[tuple[str, list[dict]]]) -> list[dict]:
    """Extracted elements whose text contains a passage left out elsewhere (not as a duplicate).

    folders: [(folder id, docs)]. Same rule as the Personal Data Report's hold-out, so the
    reviewer sees exactly what the report is holding back.
    """
    left = []
    for fid, docs in folders:
        for d in docs:
            for el in d["elements"]:
                dec = d["decisions"].get(el["id"])
                for r in (dec or {}).get("redactions", []):
                    if r["reason"] in _DUP_REASONS:
                        continue
                    n = _norm(d["text"][r["start"]:r["end"]])
                    if len(n) >= CONFLICT_MIN_CHARS:
                        left.append({"fid": fid, "file": d["name"], "element": el["id"], "redaction": r["id"],
                                     "reason": r["reason"], "norm": n})
    out = []
    for fid, docs in folders:
        for d in docs:
            for el in d["elements"]:
                dec = d["decisions"].get(el["id"])
                if not dec or dec["decision"] == "redact_full":
                    continue
                text = _norm(extract_text(d["text"], el, dec["redactions"]))
                hit = next((x for x in left if (x["fid"], x["file"]) != (fid, d["name"]) and x["norm"] in text), None)
                if hit:
                    out.append({"a": {"fid": fid, "file": d["name"], "element": el["id"]},
                                "b": {k: hit[k] for k in ("fid", "file", "element", "redaction", "reason")}})
    return out


def repeat_sources(folders: list[tuple[Path, list[dict]]]) -> dict[str, dict]:
    """What a repeat of each decided text should get, keyed by exact text ("=") and normalised text ("~").

    Follows the reviewer's own practice: text extracted somewhere (or only ever left out as a
    duplicate) is in the report, so a repeat is left out as a duplicate. Text left out for one
    real reason gets that reason. Text extracted in one place and left out for a real reason in
    another, or left out for different reasons, is ambiguous: never copied. A partial decision is
    copied only when every partial copy made the same cuts.
    """
    copies: dict[str, list[dict]] = {}
    for path, docs in folders:
        for d in docs:
            for el in d["elements"]:
                dec = d["decisions"].get(el["id"])
                raw = _element_raw(d["text"], el)
                key = _norm(raw)
                if not dec or not key:
                    continue
                c = {"from": f"{path.name}/{d['name']}", "element": el["id"], "decision": dec["decision"], "text": raw[:1200],
                     "was": {"decision": dec["decision"], "reasons": sorted({r["reason"] for r in dec["redactions"]}),
                             "reviewer": dec["reviewer"], "ts": dec["ts"], "copied": bool(dec.get("copied_from")),
                             "score": (el.get("score") or {}).get("score"),
                             "segments": decided_segments(d["text"], el, dec)},
                     "redactions": dec["redactions"], "start": el["parts"][0][0], "single": len(el["parts"]) == 1}
                for k in ("=" + raw, "~" + key) if len(key) >= REPEAT_MIN_CHARS else ("=" + raw,):
                    copies.setdefault(k, []).append(c)
    out = {}
    for k, cs in copies.items():
        released = [c for c in cs if c["decision"] == "release"]
        full = [c for c in cs if c["decision"] == "redact_full"]
        partial = [c for c in cs if c["decision"] == "redact_partial"]
        real = {r["reason"] for c in full for r in c["redactions"]} - _DUP_REASONS
        if partial:
            cuts = {tuple((r["start"] - c["start"], r["end"] - c["start"], r["reason"]) for r in c["redactions"])
                    for c in partial}
            ok = len(cuts) == 1 and not released and not real and all(c["single"] for c in partial)
            out[k] = {**partial[0], "kind": "partial"} if ok else {"ambiguous": True}
        elif released and real:
            out[k] = {"ambiguous": True}                     # extracted here, left out for a real reason there
        elif released or not real:
            src = (released or full)[0]
            out[k] = {**src, "kind": "duplicate", "decision": "redact_full",
                      "redactions": [{"reason": DUPLICATE, "note": f"Same text {'extracted' if released else 'left out as a duplicate'} in {src['from']}"}]}
        elif len(real) == 1:
            src = next(c for c in full if {r["reason"] for r in c["redactions"]} & real)
            out[k] = {**src, "kind": "same"}
        else:
            out[k] = {"ambiguous": True}
    return out


def repeats_for(docs: list[dict], sources: dict[str, dict]) -> dict[str, dict]:
    """For each file, its undecided elements that repeat a decided one: {file: {element id: decision to copy}}.

    A partial decision is copied only onto exactly the same text (its cut positions move with it).
    """
    out: dict[str, dict] = {}
    for d in docs:
        for el in d["elements"]:
            if el["id"] in d["decisions"] or el.get("below"):
                continue
            raw = _element_raw(d["text"], el)
            key = _norm(raw)
            if not key:
                continue
            exact = sources.get("=" + raw)
            src = exact or (sources.get("~" + key) if len(key) >= REPEAT_MIN_CHARS else None)
            if not src or src.get("ambiguous"):
                continue
            info = {"from": src["from"], "element": src["element"], "decision": src["decision"], "kind": src["kind"],
                    "text": screen_text(src.get("text", "")), "was": src.get("was", {})}
            if src["decision"] == "redact_full":
                r = src["redactions"][0]
                info.update(reason=r["reason"], note=r.get("note", ""))
            else:
                if not (exact and len(el["parts"]) == 1):
                    continue
                shift = el["parts"][0][0] - src["start"]
                info["redactions"] = [{"start": r["start"] + shift, "end": r["end"] + shift, "reason": r["reason"],
                                       "note": r.get("note", "")} for r in src["redactions"]]
            out.setdefault(d["name"], {})[el["id"]] = info
    return out


def extract_text(text: str, el: dict, redactions: list[dict]) -> str:
    """An element's extracted text: its lines kept, the parts left out removed, lines left empty dropped."""
    lines = []
    for a, b in el["parts"]:
        # A cut that spanned a line break keeps the break, so the lines around it stay apart.
        kept = "".join(piece if r is None else ("\n" if "\n" in text[max(r["start"], a):min(r["end"], b)] else "")
                       for piece, r in redacted_text(text, a, b, redactions))
        lines += [" ".join(line.split()) for line in kept.split("\n")]
    return "\n".join(line for line in lines if line)


def extracts(docs: list[dict]) -> list[dict]:
    """One extract per decided element (its text without the parts left out), identical ones merged.

    An element extracted in part stays one extract, so cutting a name out of a line
    never splits the rest into separate extracts. Undecided elements and elements left
    out in full give nothing. Order is the order first seen (file, then element).
    """
    merged: dict[str, dict] = {}
    for d in docs:
        for el in d["elements"]:
            dec = d["decisions"].get(el["id"])
            if not dec:
                continue
            text = extract_text(d["text"], el, dec["redactions"])
            key = _norm(text)
            if not key:
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
            return self._send(200, json.dumps({"reviewer": self.folder.reviewer, "docs": screen_docs(self.folder.docs()),
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
<title>Personal Data Reviewer</title>
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
.itemscore{background:var(--bg);border-left:3px solid var(--part,#c80);border-radius:6px;padding:8px 12px;margin:0 0 10px;font-size:14px}.itemscore .ev{font-size:12px;opacity:.75;margin-top:4px}.sect{display:flex;justify-content:space-between;align-items:center;width:100%;font-weight:600}.sect .cnt{font-weight:600;background:var(--bg);border-radius:99px;padding:0 8px;font-size:12px}.sect.on .cnt{color:var(--ink,#1d1d1b);background:#fff}.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0}.chip{border:1px solid var(--line);background:var(--panel);border-radius:99px;padding:2px 10px;font:inherit;font-size:12px;cursor:pointer}.chip.on{background:var(--accent,#2f5d8a);color:#fff;border-color:transparent}.chip.clear{opacity:.7}.filehead{margin:16px 0 6px;font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}.pair{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:start;border:1px solid var(--line);border-radius:8px;padding:10px;margin:6px 0;background:var(--panel)}.pair .ptxt{white-space:pre-wrap;font-size:14px;margin-top:4px}.pair .pbtn{display:flex;flex-direction:column;gap:6px;max-width:240px;font-size:13px}.pair .own{display:flex;flex-direction:column;gap:4px}.pair .own .btn{font-size:13px;padding:3px 8px}.el.pair{display:grid}.pair .ph{font-weight:600;font-size:13px;display:flex;gap:8px;align-items:center;margin-bottom:4px}.pair .side{padding:8px;border-radius:6px}.pair .side.now{background:var(--bg)}.grpn{background:#fff1b8;color:#7a5200;border-radius:99px;padding:0 8px;font-size:12px}.pair.conf .side.now{background:#fbe3e1;border-left:3px solid var(--red,#b3261e)}.pair .win{background:#d9f2e0;border-radius:2px}.pair .wout{text-decoration:line-through;color:var(--red,#b3261e);background:#fbe3e1}.pair .why{font-size:11px;color:var(--red,#b3261e);margin:0 4px}.pair .side.was{background:var(--okbg,#e6f4ea);border-left:3px solid var(--ok,#2e7d4f)}.ctx{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;display:flex;align-items:center;justify-content:center}.ctx[hidden]{display:none}.ctxbox{background:var(--panel,#fff);color:var(--ink,#1d1d1b);width:min(1000px,94vw);height:88vh;border-radius:10px;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,.3)}.ctxhead{display:flex;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line)}.ctxbody{overflow:auto;padding:12px 16px;flex:1}.ctxt{white-space:pre-wrap;font-size:14px;line-height:1.5}.ctxt mark,.ctx mark{background:#ffe58a;padding:0 1px}.ctxc{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0}.ctxc.here{border:2px solid #c80;background:#fff8e1}.ctxm{font-size:12px;color:var(--muted);margin-bottom:4px}.ctxb{margin-left:6px;font-size:11px;padding:1px 7px}.wibar{margin:0 0 10px}.wi{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;margin-left:10px;text-transform:none;letter-spacing:0;font-weight:400}.filehead .wi .btn{font-size:11px;padding:1px 8px}.itemwhy{text-transform:none;letter-spacing:0;font-weight:400;font-size:13px;color:var(--ink,#1d1d1b);margin-top:4px}.scope{background:var(--hl,#fff4d6);border-radius:8px;padding:8px 12px;margin:0 0 10px;font-size:14px}.el.p-hi{border-left:6px solid #c62828!important}.el.p-mid{border-left:6px solid #ef6c00!important}.el.p-low{border-left:6px solid #f2b705!important}.el.p-un{border-left:6px solid #9e9e9e!important}.nextbar{display:flex;gap:12px;align-items:center;justify-content:space-between;background:#fdecea;border:1px solid #f5c2c0;border-radius:8px;padding:10px 14px;margin:0 0 12px;font-size:14px}.nextbar.done{background:#e6f4ea;border-color:#b7dfc4}.lt{border-radius:99px;padding:0 8px;font-size:12px;font-weight:700;color:#fff;background:#c62828}.lt.amb{background:#ef6c00}.lt.ok{background:#2e7d4f}.form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px;padding:8px;background:var(--bg);border-radius:6px;font-size:13px}.form.top{justify-content:flex-end;margin:8px 0 10px}.form.col{flex-direction:column;align-items:flex-end}.form .frow{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:flex-end}.btn kbd{font:600 10px/1 var(--mono,monospace);border:1px solid currentColor;border-radius:3px;padding:1px 4px;margin-left:4px;opacity:.6}
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
const PAGE_FID=(location.pathname.match(/\/f\/([0-9a-f]+)\//)||[])[1]||"",TOKEN=new URLSearchParams(location.search).get("token")||(document.querySelector('meta[name="csrf"]')||{}).content,REASONS=__REASONS__,SUBJECT=(new URLSearchParams(location.search).get("subject")||"").toLowerCase();
const COLOURS=["#2f5d8a","#2e7d4f","#8a4b9c","#b5651d","#1f7a8c"];
const LABEL={release:"Extracted",redact_full:"Left out",redact_partial:"Extracted in part",pending:"Not reviewed"};
let DOCS=[],cur=0,view="original",q="",edit=null,full=null,split=null,minScore=-1,focus=null,lastReason=REASONS[0],doneOpen=false,openRow=null,REP={},CONF=[],GROUPS=[],EXPAND=new Set(),repOpen=false,lowOpen=false,MODE="review",DISMISS=new Set(),F={rs:new Set(),ls:new Set(),dd:new Set(),ir:new Set(),hw:new Set(),fd:new Set(),rr:"",sb:""};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const reasonOpts=()=>REASONS.map(x=>`<option>${esc(x)}</option>`).join("");
function toast(m){const t=document.getElementById("toast");t.textContent=m;t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",3000)}
function rk(d){return d.fid?d.fid+"/"+d.name:d.name}
function fbase(d){return d&&d.fid&&d.fid!==PAGE_FID?"../"+d.fid+"/":""}
async function load(keep){const j=await (await fetch("api/docs?all=1&token="+TOKEN)).json();
 if(j.folders){DOCS=[];REP={};for(const f of j.folders){for(const d of f.docs){d.fid=f.fid;d.folder=f.label;d.fname=f.name||f.label;DOCS.push(d)}for(const n in f.repeats)REP[f.fid+"/"+n]=f.repeats[n]}CONF=j.conflicts||[]}else{DOCS=j.docs;REP=j.repeats||{};CONF=[]}document.getElementById("who").innerHTML="Reviewer: <b>"+esc(j.reviewer)+"</b>"+(j.chain_ok?"":" · <span style='color:var(--red)'>event log chain broken</span>");if(!keep){cur=Math.max(0,DOCS.findIndex(d=>d.fid===PAGE_FID));const P=new URLSearchParams(location.search),F=P.get("file"),E=P.get("el"),S=P.get("section");if(S&&SECTIONS[S])MODE=S;if(F){let i=DOCS.findIndex(d=>d.name===F&&(!d.fid||d.fid===PAGE_FID));if(i<0)i=DOCS.findIndex(d=>d.name===F);if(i>=0){MODE="file";cur=i;if(E){openRow=E;focus=E}}}}nav();render();if(!keep&&openRow)setTimeout(()=>goTo(openRow),60);if(!focus){const d=DOCS[cur];focus=(d.elements.find(e=>!d.decisions[e.id]&&!e.blank&&!e.below&&!(REP[rk(d)]||{})[e.id])||{}).id;render()}}
async function act(body){Object.assign(body,{file:DOCS[cur].name,sha256:DOCS[cur].sha256});const r=await fetch(fbase(DOCS[cur])+"api/act",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify(body)});const j=await r.json();if(!r.ok){toast(j.error);return false}return true}
function nav(){const f=document.getElementById("files");f.innerHTML="";const c=counts();
 for(const k in SECTIONS){const b=document.createElement("button");b.className="sect"+(MODE===k?" on":"");b.innerHTML=`${SECTIONS[k]}${k==="low"||k==="done"?`<span class="cnt">${c[k]}</span>`:light(k,c[k])}`;b.onclick=()=>{MODE=k;q="";openRow=null;edit=null;full=null;split=null;focus=null;render();scrollTo(0,0)};f.appendChild(b)}
 const hd=document.createElement("div");hd.className="t";hd.style.margin="12px 0 4px";hd.textContent="By file";f.appendChild(hd);
 let lastF=null;DOCS.forEach((d,i)=>{if(d.folder&&d.folder!==lastF){const fh=document.createElement("div");fh.className="t";fh.style.margin="10px 0 2px";fh.style.fontWeight="600";fh.textContent=d.fname||d.folder;f.appendChild(fh);lastF=d.folder}const n=Object.keys(d.decisions).length,nr=Object.keys(REP[rk(d)]||{}).length,tot=d.elements.filter(e=>(!e.blank&&!e.below)||d.decisions[e.id]).length,b=document.createElement("button");b.className=MODE==="file"&&i===cur?"on":"";b.innerHTML=esc(d.name.replace(/\.txt$/,"").replace(/_/g," "))+`<span class="prog">${d.item&&d.item.score!=null?`score ${d.item.score} · `:""}${n}/${tot} reviewed · ${d.redactions.length} passage(s) left out${nr?` · ${nr} repeat(s)`:""}</span>`;b.onclick=()=>{if(edit&&!confirm("Discard unsaved marks?"))return;MODE="file";cur=i;q="";edit=null;full=null;split=null;focus=(d.elements.find(e=>!d.decisions[e.id]&&!e.blank&&!e.below&&!(REP[rk(d)]||{})[e.id])||{}).id;nav();render();scrollTo(0,0)};f.appendChild(b)})}
function hl(s){let e=esc(s);if(q){const re=new RegExp("("+q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","gi");e=e.replace(re,"<mark>$1</mark>")}return e}
// One part [a,b] of an element, with redactions rs; cls is the class for released text
function piece(d,a,b,rs,cls,editable){let out="",pos=a;const seg0=(x,y)=>`<span data-s="${x}" class="${cls}">${hl(d.text.slice(x,y))}</span>`;
 const seg=(x,y)=>split&&split.at>x&&split.at<y?seg0(x,split.at)+`<span class="cutpt"></span>`+seg0(split.at,y):seg0(x,y);
 for(const r of rs.filter(r=>r.end>a&&r.start<b).sort((x,y)=>x.start-y.start)){const s=Math.max(r.start,a),e=Math.min(r.end,b);if(s<pos)continue;if(s>pos)out+=seg(pos,s);
  if(editable)out+=`<span class="mark" data-s="${s}" data-i="${r.i}" title="${esc(r.reason)} (click to unmark)">${hl(d.text.slice(s,e))}</span>`;
  else if(view==="redacted")out+=`<span class="blk">█ Left out: ${esc(r.reason)}</span>`;
  else out+=`<span class="cut" data-s="${s}" title="${esc(r.reason)}${r.note?": "+esc(r.note):""}">${hl(d.text.slice(s,e))}</span><span class="why">[${esc(r.reason)}]</span>`;pos=e}
 if(pos<b)out+=seg(pos,b);return out}
// One line for a processed element; click it to open the full element.
function row(d,el){const dec=d.decisions[el.id],st=dec.decision,text=el.parts.map(([a,b])=>d.text.slice(a,b)).join(" ").replace(/\s+/g," ").trim();
 return `<div class="row" data-row="${el.id}" title="Click to review or change"><b>${esc(el.who||el.label)}</b><span class="t">${esc(el.t||"")}</span><span class="badge ${st}">${LABEL[st]}${st==="redact_partial"?": "+dec.redactions.length:""}</span><span class="snip">${esc(text.slice(0,140))}</span></div>`}
// The three decision buttons, on every element; the current decision is shown pressed.
function buttons(st){return `<button class="btn big ${st==="release"?"go":""}" data-a="approve" title="A">${st==="release"?"✓ ":""}Extract <kbd>E</kbd></button><button class="btn big ${st==="redact_full"?"stop":""}" data-a="full" title="R">${st==="redact_full"?"✓ ":""}Leave out <kbd>L</kbd></button><button class="btn big ${st==="redact_partial"?"primary":""}" data-a="partial" title="P">${st==="redact_partial"?"✓ Edit parts <kbd>P</kbd>":"Leave part out <kbd>P</kbd>"}</button>`}
function more(el,decided){return `<details class="more"><summary title="More">⋯</summary><div class="menu"><button data-a="split">Split…</button>${el.split?'<button data-a="unsplit">Undo split</button>':""}${decided?'<button data-a="change">Clear decision</button>':""}</div></details>`}
function cardHTML(d,el,key,who){
  const dec=d.decisions[el.id],st=dec?dec.decision:"pending",editing=edit&&edit.el===key,c=el.who?COLOURS[who.indexOf(el.who)%COLOURS.length]:"var(--muted)";
  const subj=el.who&&SUBJECT&&el.who.toLowerCase().startsWith(SUBJECT);
  const splitting=split&&split.el===key;let body,ctl;
  if(editing){const rs=edit.marks.map((r,i)=>({...r,i}));body=el.parts.map(([a,b])=>piece(d,a,b,rs,"",true)).join(" ");
   ctl=`<span class="badge redact_partial">Marking parts: ${edit.marks.length}</span><span class="sp"></span><button class="btn go" data-a="save">Save</button><button class="btn" data-a="cancel">Cancel</button>`}
  else if(dec){body=el.parts.map(([a,b])=>piece(d,a,b,dec.redactions,st==="release"||st==="redact_partial"?"rel":"",false)).join(" ");
   if(st==="redact_full"&&view==="redacted")body=`<span class="blk">█ Left out: ${esc(dec.redactions[0]?.reason||"")}</span>`;
   ctl=`<span class="badge ${st}">${LABEL[st]}${st==="redact_partial"?": "+dec.redactions.length+" part(s)":""}${st==="redact_full"?": "+esc(dec.redactions[0]?.reason||""):""}</span><span class="t">${esc(dec.reviewer)}, ${esc(dec.ts.slice(0,16).replace("T"," "))}</span><span class="sp"></span>${buttons(st)}${more(el,true)}`}
  else{body=el.parts.map(([a,b])=>piece(d,a,b,[],"",false)).join(" ");
   ctl=`<span class="badge pending">Not reviewed</span><span class="sp"></span>${buttons(st)}${more(el,false)}`}
  const prior=(d.earlier[el.id]||[]).map(p=>`${LABEL[p.decision]}${p.reasons.length?" ("+esc(p.reasons.join(", "))+")":""}, ${esc(p.reviewer)} ${esc(p.ts.slice(0,16).replace("T"," "))}`).join("; ");
  let form="",top="";
  if(editing)top=`<div class="form top">Select text below to mark it. Reason for new marks: <select id="er">${reasonOpts()}</select><input id="en" placeholder="Note (required for Other)" value="${esc(edit.note||"")}"></div>`;
  if(splitting){const opts=[...who,"Unknown speaker"].map(w=>`<option ${w===split.who?"selected":""}>${esc(w)}</option>`).join("");
   form=`<div class="form">${split.at==null?"Click in the text where the new part should start.":"New part starts at the blue line. Speaker of the new part:"} <select id="sw">${opts}</select><button class="btn primary" data-a="splitok" ${split.at==null?"disabled":""}>Split here</button><button class="btn" data-a="splitcancel">Cancel</button></div>`}
  if(full===key)top=`<div class="form top col"><div class="frow"><button class="btn stop" data-a="fullok">Leave out (Enter)</button><button class="btn" data-a="fullcancel">Cancel (Esc)</button></div><div class="frow">Leave all of this element out. Reason: <select id="fr">${REASONS.map(x=>`<option ${x===lastReason?"selected":""}>${esc(x)}</option>`).join("")}</select><input id="fn" placeholder="Note (required for Other)"></div></div>`;
  const s=el.score,chip=s?(s.score==null?`<span class="sc lo" title="${esc(s.special||s.description||"")}">Model: –</span>`:`<span class="sc ${s.score>=7?"hi":s.score>=5?"mid":"lo"}" title="Model suggestion, not a decision: ${esc(s.description||"")}${s.evidence?" | "+esc(s.evidence):""}">Model ${s.score}</span>`):"";
  const name=el.who?`<b>${esc(el.who)}${el.split?" <span class='t'>(split by reviewer)</span>":""}</b><span class="t">${esc(el.t||"")}</span>`:`<b style="color:var(--muted)">${esc(el.label)}</b>`;
  return `<div class="el ${st} ${prio(el,dec)} ${subj?"subj":""} ${editing?"editing":""} ${splitting?"editing splitting":""} ${focus===key?"focus":""}" data-el="${key}" style="--c:${c}"><div class="h">${name}${chip}<button class="btn sm ctxb" data-ctx type="button" title="See the whole document, item and conversation">Context</button>${ctl}</div>${top}${prior?`<div class="prior">Decided before this element was split: ${prior}. Please decide again.</div>`:""}<div class="txt">${body}</div>${form}</div>`}
function render(){if(MODE!=="file")return renderSection();const d=DOCS[cur],m=document.getElementById("main");if(!d){m.innerHTML="<p>No .txt files in this folder.</p>";return}
 const who=[...new Set(d.elements.map(e=>e.who).filter(Boolean))],n=Object.keys(d.decisions).length,total=d.elements.filter(e=>(!e.blank&&!e.below)||d.decisions[e.id]).length;
 let h=`<div class="bar"><input type="search" id="q" placeholder="Search…" value="${esc(q)}"><span class="toggle"><button data-v="original" class="${view==="original"?"on":""}">Original</button><button data-v="redacted" class="${view==="redacted"?"on":""}">As extracted</button></span>
  <select id="min" class="btn" title="Show only elements the model scored at or above this"><option value="-1">All scores</option>${[1,5,6,7,8].map(n=>`<option value="${n}" ${minScore===n?"selected":""}>Model score ≥ ${n}</option>`).join("")}</select><button class="btn" id="all">Extract all not yet reviewed</button><button class="btn primary" id="exp">Export redacted copy</button></div>
  <div class="legend"><span><i class="sw" style="background:var(--okbg);border:1px solid var(--ok)"></i>Extracted</span><span><i class="sw" style="background:var(--redbg);border:1px solid var(--red)"></i>Left out</span><span>${n} of ${total} elements reviewed</span><span class="keys"><kbd>E</kbd> extract · <kbd>L</kbd> leave out · <kbd>P</kbd> leave part out · <kbd>↓</kbd><kbd>↑</kbd> move · <kbd>Enter</kbd> confirm</span></div>`;
 if(d.item)h+=`<div class="itemscore">Classified as a whole item: <b>model score ${d.item.score??"–"}</b>${d.item.reason?" · "+esc(d.item.reason):""}${d.item.evidence?`<div class="ev">Evidence: “${esc(d.item.evidence)}”</div>`:""}</div>`;
 h+=wiControls(d,cur)?`<div class="wibar">${wiControls(d,cur)}</div>`:"";
 if(d.stale)h+=`<div class="warn">${d.stale} earlier decision(s) were made on a different version of this file and are not shown.</div>`;
 const cards={},shown=[];
 for(const el of d.elements){if(el.blank&&!d.decisions[el.id])continue;const text=el.parts.map(([a,b])=>d.text.slice(a,b)).join(" ");
  if(q&&!text.toLowerCase().includes(q.toLowerCase())&&!(el.who||"").toLowerCase().includes(q.toLowerCase())&&edit?.el!==el.id)continue;
  if(minScore>=0&&!(el.score&&el.score.score>=minScore)&&edit?.el!==el.id&&split?.el!==el.id)continue;
  shown.push(el);
  cards[el.id]=cardHTML(d,el,el.id,who)}
 // Processed elements collapse into one bar at the top; the rest follow in document order.
 const active=id=>edit?.el===id||full===id||split?.el===id||openRow===id;
 const R=REP[rk(d)]||{},done=shown.filter(el=>d.decisions[el.id]),todo=shown.filter(el=>!d.decisions[el.id]&&!R[el.id]&&!el.below),reps=shown.filter(el=>!d.decisions[el.id]&&R[el.id]),low=shown.filter(el=>!d.decisions[el.id]&&el.below);
 if(done.length){const cnt={};done.forEach(el=>{const k=d.decisions[el.id].decision;cnt[k]=(cnt[k]||0)+1});
  h+=`<div class="done"><button class="dbar" id="dtoggle">${doneOpen?"▾":"▸"} Processed: ${done.length}<span class="t">${Object.entries(cnt).map(([k,v])=>v+" "+LABEL[k].toLowerCase()).join(" · ")}</span><span class="sp"></span><span class="t">${doneOpen?"Hide":"Show to review or change"}</span></button>`;
  h+=doneOpen?done.map(el=>active(el.id)?cards[el.id]:row(d,el)).join(""):done.filter(el=>active(el.id)).map(el=>cards[el.id]).join("");
  h+=`</div>`}
 if(reps.length){h+=`<div class="done rep"><div class="dbar"><button class="btn primary" id="applyrep">Apply the same decisions to ${reps.length} repeat(s)</button><span class="t">Same text as elements you have already decided</span><span class="sp"></span><button class="btn" id="rtoggle">${repOpen?"Hide":"Show them"}</button></div>`;
  h+=repOpen?reps.map(el=>active(el.id)?cards[el.id]:`<div class="row" data-row="${el.id}"><span class="badge pending">Repeat</span><span class="t">${R[el.id].kind==="duplicate"?"Duplicate of "+esc(R[el.id].from):esc(R[el.id].from)+": "+LABEL[R[el.id].decision]+(R[el.id].reason?" ("+esc(R[el.id].reason)+")":"")}</span> ${esc(el.parts.map(([a,b])=>d.text.slice(a,b)).join(" ").replace(/\s+/g," ").slice(0,140))}</div>`).join(""):reps.filter(el=>active(el.id)).map(el=>cards[el.id]).join("");
  h+=`</div>`}
 if(low.length){h+=`<div class="done low"><button class="dbar" id="ltoggle">${lowOpen?"▾":"▸"} Below 7: ${low.length}<span class="t">the model scored these under 7, so they are not reviewed and stay out of the report</span><span class="sp"></span><span class="t">${lowOpen?"Hide":"Show"}</span></button>`;
  h+=lowOpen?low.map(el=>cards[el.id]).join(""):low.filter(el=>active(el.id)).map(el=>cards[el.id]).join("");h+=`</div>`}
 h+=todo.length?todo.map(el=>cards[el.id]).join(""):reps.length?`<p class="alldone">Only repeats are left: apply the same decisions above, or open one to decide it differently.</p>`:`<p class="alldone">Every element ${q||minScore>=0?"shown here ":""}is reviewed. Open “Processed” to check them, or export the redacted copy.</p>`;
 m.innerHTML=h;
 if(edit){const s=document.getElementById("er");if(s)s.value=edit.reason||REASONS[0]}
 const qi=document.getElementById("q");qi.oninput=()=>{q=qi.value;const p=qi.selectionStart;render();const n=document.getElementById("q");n.focus();n.setSelectionRange(p,p)};
 m.querySelectorAll(".toggle button").forEach(b=>b.onclick=()=>{view=b.dataset.v;render()});
 const dt=document.getElementById("dtoggle");if(dt)dt.onclick=()=>{doneOpen=!doneOpen;openRow=null;render()};
 const lt=document.getElementById("ltoggle");if(lt)lt.onclick=()=>{lowOpen=!lowOpen;openRow=null;render()};
 const rt=document.getElementById("rtoggle");if(rt)rt.onclick=()=>{repOpen=!repOpen;openRow=null;render()};
 const ar=document.getElementById("applyrep");if(ar)ar.onclick=async()=>{ar.disabled=true;await applyRepeats(reps.map(el=>({fid:d.fid,file:d.name,element:el.id})))};
 document.getElementById("min").onchange=ev=>{minScore=+ev.target.value;render()};
 document.getElementById("exp").onclick=()=>window.open(fbase(d)+"export?token="+TOKEN+"&file="+encodeURIComponent(d.name),"_blank");
 document.getElementById("all").onclick=async()=>{const todo=d.elements.filter(e=>!d.decisions[e.id]&&!e.blank&&!e.below&&!(REP[rk(d)]||{})[e.id]);if(!todo.length||!confirm(`Extract ${todo.length} element(s) not yet reviewed, as they are?`))return;for(const e of todo)if(!await act({action:"decide",element:e.id,decision:"release"}))break;await load(true)};
 wire(m);
}
function wire(m){wireWI(m);
 m.querySelectorAll("[data-ctx]").forEach(b=>b.onclick=ev=>{ev.stopPropagation();const [fi,id]=splitKey(b.closest(".el").dataset.el);showContext(DOCS[fi],id)});
 m.querySelectorAll("[data-ctx2]").forEach(b=>b.onclick=ev=>{ev.stopPropagation();ctxFor(b.dataset.ctx2)});
 m.querySelectorAll("[data-a]").forEach(b=>b.onclick=async()=>{const key=b.closest(".el").dataset.el,[fi,id]=splitKey(key),a=b.dataset.a;cur=fi;
  if(a==="approve"){if(DOCS[cur].decisions[id]?.decision==="release")return advance(key);if(await act({action:"decide",element:id,decision:"release"})){await load(true);advance(key)}}
  else if(a==="full"){if(edit)return toast("Save or cancel the element you are marking first");focus=key;full=key;render()}
  else if(a==="fullcancel"){full=null;render()}
  else if(a==="fullok"){lastReason=document.getElementById("fr").value;if(await act({action:"decide",element:id,decision:"redact_full",reason:lastReason,note:document.getElementById("fn").value})){full=null;await load(true);advance(key)}}
  else if(a==="partial"){if(edit)return toast("Save or cancel the element you are marking first");const prev=DOCS[cur].decisions[id];view="original";full=null;split=null;
   edit={el:key,marks:prev&&prev.decision==="redact_partial"?prev.redactions.map(r=>({start:r.start,end:r.end,reason:r.reason,note:r.note})):[],reason:REASONS[0]};render()}
  else if(a==="cancel"){edit=null;render()}
  else if(a==="save"){if(await act({action:"decide",element:id,decision:"redact_partial",redactions:edit.marks})){edit=null;await load(true);advance(key)}}
  else if(a==="split"){if(edit)return toast("Save or cancel the element you are marking first");full=null;split={el:key,at:null,who:"Unknown speaker"};render()}
  else if(a==="splitcancel"){split=null;render()}
  else if(a==="splitok"){const w=document.getElementById("sw").value;if(await act({action:"split",at:split.at,who:w==="Unknown speaker"?"Unknown speaker":w})){split=null;await load(true)}}
  else if(a==="unsplit"){if(confirm("Undo this split? Decisions on either part will need to be made again."))if(await act({action:"unsplit",at:+id.slice(1)}))await load(true)}
  else if(a==="change"){if(confirm("Clear this element's decision? It becomes not reviewed (the earlier decision stays in the log)."))if(await act({action:"reset",element:id}))await load(true)}});
 m.querySelectorAll(".el").forEach(x=>x.addEventListener("mousedown",ev=>{if(!ev.target.closest("button,select,input,summary"))focus=x.dataset.el;m.querySelectorAll(".el.focus").forEach(y=>y.classList.remove("focus"));x.classList.add("focus")}));
 const fr=document.getElementById("fr");if(fr){fr.focus();fr.onkeydown=ev=>{if(ev.key==="Enter"){ev.preventDefault();m.querySelector('[data-a="fullok"]').click()}}}
 m.querySelectorAll(".mark").forEach(s=>s.onclick=()=>{edit.marks.splice(+s.dataset.i,1);render()});
 const er=document.getElementById("er"),en=document.getElementById("en");if(er)er.onchange=()=>edit.reason=er.value;if(en)en.oninput=()=>edit.note=en.value;
 m.querySelectorAll(".row").forEach(r=>r.onclick=()=>{openRow=r.dataset.row;focus=openRow;render()});
}
function splitKey(k){if(typeof k==="string"&&k.includes("|")){const [a,b]=k.split("|");return [+a,b]}return [cur,k]}
function undecidedKey(k){const [fi,id]=splitKey(k);return !DOCS[fi].decisions[id]}

// ---- Section views: every file of the folder at once -------------------------------------------
// review: undecided, scored 7+ (or not yet scored) | low: undecided, scored below 7
// repeats: undecided, same text as a decided element | done: every decided element
const SECTIONS={review:"To review",low:"Below 7 (no decision)",repeats:"Repeats",conflicts:"Conflicts",done:"Reviewed"};
const DUP="Duplicate – already in the Personal Data Report";
function di(fid,file){return DOCS.findIndex(d=>(d.fid||"")===(fid||"")&&d.name===file)}
function sc(el){const s=el.score;return s&&s.score!=null?s.score:null}
function pool(kind){const out=[];DOCS.forEach((d,fi)=>{const R=REP[rk(d)]||{};d.elements.forEach(el=>{if(el.blank&&!d.decisions[el.id])return;
 const dec=d.decisions[el.id],rep=!dec&&R[el.id]&&!DISMISS.has(fi+"|"+el.id);
 const k=dec?"done":rep?"repeats":el.below?"low":"review";if(k===kind)out.push({d,fi,el,dec,rep:rep?R[el.id]:null})})});return out}
function counts(){const c={};for(const k in SECTIONS)c[k]=k==="conflicts"?CONF.length:pool(k).length;return c}
function chipRow(name,values,sel,label){return `<div class="chips"><span class="t">${label}</span>${values.map(v=>`<button class="chip ${sel.has(String(v))?"on":""}" data-chip="${name}" data-v="${v}">${v==="none"?"not scored":v}</button>`).join("")}<button class="chip clear" data-chip="${name}" data-v="*">all</button></div>`}
function pairHTML(x,key,grp){const txt=x=>x.el.parts.map(([a,b])=>x.d.text.slice(a,b)).join(" ");
const r=x.rep;
   const w=r.was||{},ms=s=>s==null?`<span class="sc lo">Model –</span>`:`<span class="sc ${s>=7?"hi":s>=5?"mid":"lo"}">Model ${s}</span>`;
   return `<div class="el pair ${focus===key?"focus":""}" data-key="${key}" data-el="${key}">
    <div class="side was"><div class="ph">✓ Your earlier decision ${ms(w.score)}<button class="btn sm ctxb" data-ctx2="${esc(r.from.split("/")[0])}|${esc(r.from.split("/").slice(1).join("/"))}|${r.element}">Context</button></div>
     <div class="t"><span class="badge ${w.decision}">${LABEL[w.decision]||""}</span>${(w.reasons||[]).length?" "+esc(w.reasons.join(", ")):""} · ${esc(w.reviewer||"")} ${esc((w.ts||"").slice(0,16).replace("T"," "))}${w.copied?" (copied)":""}<br>in ${esc(r.from)}</div>
     <div class="ptxt">${(w.segments||[]).map(g=>g.out?`<span class="wout" title="${esc(g.reason||"")}">${esc(g.text)}</span><span class="why">[${esc(g.reason||"")}]</span>`:`<span class="win">${esc(g.text)}</span>`).join("")||hl(r.text||"")}</div></div>
    <div class="side now"><div class="ph">${grp?`Repeat · <span class="grpn">×${grp.n} copies in ${grp.files} file(s)</span>`:"Repeat · to decide now"} ${ms(sc(x.el))}<button class="btn sm ctxb" data-ctx2="${x.d.fid||""}|${esc(x.d.name)}|${x.el.id}">Context</button></div><div class="t">${grp?"first copy: ":""}${x.d.folder?esc(x.d.folder.split(" › ").pop())+" › ":""}${esc(x.d.name.replace(/\.txt$/,""))}</div><div class="ptxt">${r.redactions?x.el.parts.map(([a,b])=>piece(x.d,a,b,r.redactions.map((m,i)=>({...m,i})),"rel",false)).join(" "):hl(txt(x))}</div>${r.redactions?`<div class="t">Struck text shows the cuts Confirm would apply.</div>`:""}</div>
    <div class="pbtn"><div class="t">Confirm records for the repeat:<br><span class="badge ${r.decision}">${LABEL[r.decision]}</span>${r.reason?" "+esc(r.reason):""}</div>${grp?`<button class="btn go" data-rep="okall" data-gk="${esc(grp.k)}">Confirm all ${grp.n}</button><button class="btn" data-rep="noall" data-gk="${esc(grp.k)}">Not a match (all ${grp.n})</button><button class="btn" data-expand="${esc(grp.k)}">Show each copy</button>`:`<button class="btn go" data-rep="ok">Confirm match</button><button class="btn" data-rep="no">Not a match</button><div class="t" style="margin-top:6px">Or decide it yourself:</div><div class="own">${buttons("pending")}</div>`}</div></div>`}
function normT(s){return s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu," ").trim()}
function renderConflicts(m){
 let h=`<div class="bar"><b style="font-size:16px">Conflicts</b><span class="t">the same text extracted in one place and left out in another; the report holds these back until you choose</span></div>`;
 if(!CONF.length)h+=`<p class="alldone">No conflicts: nothing is held back.</p>`;
 CONF.forEach((c,i)=>{const ai=di(c.a.fid,c.a.file),bi=di(c.b.fid,c.b.file);if(ai<0||bi<0)return;const A=DOCS[ai],B=DOCS[bi];
  const ea=A.elements.find(e=>e.id===c.a.element),eb=B.elements.find(e=>e.id===c.b.element);if(!ea||!eb)return;const da=A.decisions[ea.id],db=B.decisions[eb.id];
  const lab=d=>`${d.folder?esc(d.folder)+" › ":""}${esc(d.name.replace(/\.txt$/,"").replace(/_/g," "))}`;
  h+=`<div class="pair conf" data-ci="${i}">
   <div class="side was"><div class="ph">Extracted here ${ea.score&&ea.score.score!=null?`<span class="sc hi">Model ${ea.score.score}</span>`:""}<button class="btn sm ctxb" data-ctx2="${c.a.fid}|${esc(c.a.file)}|${ea.id}">Context</button></div><div class="t">${lab(A)} · ${esc(ea.label)} · <span class="badge ${da.decision}">${LABEL[da.decision]}</span></div><div class="ptxt">${ea.parts.map(([a,b])=>piece(A,a,b,da.redactions,"rel",false)).join(" ")}</div></div>
   <div class="side now"><div class="ph">Left out here ${eb.score&&eb.score.score!=null?`<span class="sc">Model ${eb.score.score}</span>`:""}<button class="btn sm ctxb" data-ctx2="${c.b.fid}|${esc(c.b.file)}|${eb.id}">Context</button></div><div class="t">${lab(B)} · ${esc(eb.label)} · <span class="badge ${db.decision}">${LABEL[db.decision]}</span> “${esc(c.b.reason)}”</div><div class="ptxt">${eb.parts.map(([a,b])=>piece(B,a,b,db.redactions,"",false)).join(" ")}</div></div>
   <div class="pbtn"><button class="btn go" data-cf="dup">Left-out copy is a duplicate</button><div class="t">changes its reason to “Duplicate – already in the Personal Data Report”; the extract goes in</div>
    <button class="btn stop" data-cf="same">Leave it out here too</button><div class="t">the extracted copy is left out (“${esc(c.b.reason)}”)</div>
    <button class="btn" data-cf="openA">Open extracted copy</button><button class="btn" data-cf="openB">Open left-out copy</button></div></div>`});
 m.innerHTML=h;
 m.querySelectorAll("[data-cf]").forEach(b=>b.onclick=async()=>{const c=CONF[+b.closest(".pair").dataset.ci],k=b.dataset.cf;
  const ai=di(c.a.fid,c.a.file),bi=di(c.b.fid,c.b.file);
  if(k==="openA"||k==="openB"){const x=k==="openA"?c.a:c.b,i=k==="openA"?ai:bi;MODE="file";cur=i;openRow=x.element;focus=x.element;nav();render();setTimeout(()=>goTo(x.element),60);return}
  b.disabled=true;
  if(k==="dup"){cur=bi;const dec=DOCS[bi].decisions[c.b.element],note=`Was “${c.b.reason}”: the same text is extracted in ${c.a.file}`;
   const ok=dec.decision==="redact_full"?await act({action:"decide",element:c.b.element,decision:"redact_full",reason:DUP,note})
    :await act({action:"decide",element:c.b.element,decision:"redact_partial",redactions:dec.redactions.map(r=>({start:r.start,end:r.end,reason:r.id===c.b.redaction?DUP:r.reason,note:r.id===c.b.redaction?note:(r.note||"")}))});
   if(ok)toast("Recorded: the left-out copy is a duplicate")}
  else{cur=ai;if(await act({action:"decide",element:c.a.element,decision:"redact_full",reason:c.b.reason,note:`Left out to match ${c.b.file}`}))toast("Recorded: left out here too")}
  await load(true);nav()});
 wire(m);nav()}
function renderSection(){const m=document.getElementById("main"),who=[];if(MODE==="conflicts")return renderConflicts(m);
 let items=pool(MODE);
 const byF={};for(const x of items){const n=x.d.fname||"this folder";byF[n]=byF[n]||{items:new Set(),n:0};byF[n].items.add(rk(x.d));byF[n].n++}
 const nItems=new Set(items.map(x=>rk(x.d))).size;
 let h=`<div class="scope"><b>${SECTIONS[MODE]}: ${items.length} paragraph(s) in ${nItems} item(s)</b>${Object.keys(byF).length?" — "+Object.entries(byF).map(([k,v])=>`${esc(k)}: ${v.items.size} item(s), ${v.n} paragraph(s)`).join(" · "):""}</div>
 <div class="bar"><b style="font-size:16px">${SECTIONS[MODE]}</b><span class="t">${MODE==="review"?"highest score first · ":""}everything in this review, across all folders</span><input type="search" id="q" placeholder="Search…" value="${esc(q)}"></div>`;
 const fname=d=>(d.folder||"").split(" › ").pop();
 {const fv=[...new Set(pool(MODE).map(x=>fname(x.d)))].filter(Boolean).sort();if(fv.length>1){h+=`<div class="chips"><span class="t">Folder</span>${fv.map(v=>`<button class="chip ${F.fd.has(v)?"on":""}" data-chip="fd" data-v="${esc(v)}">${esc(v)} (${pool(MODE).filter(x=>fname(x.d)===v).length})</button>`).join("")}<button class="chip clear" data-chip="fd" data-v="*">all</button></div>`}
  if(F.fd.size)items=items.filter(x=>F.fd.has(fname(x.d)))}
 // Highest scores first (paragraph score, then the item's own score); unscored last, document order within.
 if(MODE==="review"||MODE==="low"){const k=x=>[sc(x.el)??-1,(x.d.item&&x.d.item.score)??-1];items.sort((a,b)=>{const A=k(a),B=k(b);return B[0]-A[0]||B[1]-A[1]})}
 const qq=q.toLowerCase(),txt=x=>x.el.parts.map(([a,b])=>x.d.text.slice(a,b)).join(" ");
 if(qq)items=items.filter(x=>txt(x).toLowerCase().includes(qq)||x.d.name.toLowerCase().includes(qq));
 if(MODE==="review"){const vals=[...new Set(pool("review").map(x=>sc(x.el)===null?"none":String(sc(x.el))))].sort();h+=chipRow("rs",vals,F.rs,"Model score");if(F.rs.size)items=items.filter(x=>F.rs.has(sc(x.el)===null?"none":String(sc(x.el))))}
 if(MODE==="low"){const vals=[...new Set(pool("low").map(x=>String(sc(x.el))))].sort((a,b)=>a-b);h+=chipRow("ls",vals,F.ls,"Model score");if(F.ls.size)items=items.filter(x=>F.ls.has(String(sc(x.el))));
  h+=`<p class="t">Scored below 7: no decision needed and nothing goes in the report. Extract one if it should be in.</p>`}
 if(MODE==="done"){const all=pool("done"),dv=[...new Set(all.map(x=>x.dec.decision))],rv=[...new Set(all.flatMap(x=>x.dec.redactions.map(r=>r.reason)))];
  h+=`<div class="chips"><span class="t">Decision</span>${dv.map(v=>`<button class="chip ${F.dd.has(v)?"on":""}" data-chip="dd" data-v="${v}">${LABEL[v]}</button>`).join("")}<button class="chip clear" data-chip="dd" data-v="*">all</button></div>`;
  h+=`<div class="chips"><span class="t">In the report</span>${["yes","no"].map(v=>`<button class="chip ${F.ir.has(v)?"on":""}" data-chip="ir" data-v="${v}">${v==="yes"?"included":"not included"}</button>`).join("")}<span class="t" style="margin-left:12px">How</span>${["by hand","copied"].map(v=>`<button class="chip ${F.hw.has(v)?"on":""}" data-chip="hw" data-v="${v}">${v==="copied"?"copied from a repeat":"decided by hand"}</button>`).join("")}<button class="chip clear" data-chip="ir,hw" data-v="*">all</button></div>`;
  h+=`<div class="chips"><span class="t">Reason</span><select id="rsel" class="btn"><option value="">any</option>${rv.map(v=>`<option ${F.rr===v?"selected":""}>${esc(v)}</option>`).join("")}</select><span class="t" style="margin-left:12px">Model score</span><select id="ssel" class="btn"><option value="">any</option>${["7+","below 7","not scored"].map(v=>`<option ${F.sb===v?"selected":""}>${v}</option>`).join("")}</select></div>`;
  if(F.dd.size)items=items.filter(x=>F.dd.has(x.dec.decision));
  if(F.ir.size)items=items.filter(x=>F.ir.has(x.dec.decision==="redact_full"?"no":"yes"));
  if(F.hw.size)items=items.filter(x=>F.hw.has(x.dec.copied_from?"copied":"by hand"));
  if(F.rr)items=items.filter(x=>x.dec.redactions.some(r=>r.reason===F.rr));
  if(F.sb)items=items.filter(x=>{const s=sc(x.el);return F.sb==="7+"?s!==null&&s>=7:F.sb==="below 7"?s!==null&&s<7:s===null})}
 if(MODE==="repeats"&&items.length)h+=`<div class="bar"><button class="btn primary" id="confirmall">Confirm all ${items.length} shown</button><span class="t">Your earlier decision is on the left, the repeat to decide on the right. Confirm to record the same outcome under your name; “Not a match” moves it back to be reviewed.</span></div>`;
 if(MODE==="review"){const top=items[0];
  h+=items.length?`<div class="nextbar"><div><b>Next step:</b> decide ${items.length} paragraph(s), most important first${top&&sc(top.el)!=null?` (highest score ${sc(top.el)})`:""}. Red = 9–10, orange = 8, amber = 7, grey = not scored.</div><button class="btn primary" id="gofirst">Start with the most important →</button></div>`
   :`<div class="nextbar done"><b>✓ Nothing left to review.</b> ${counts().repeats?"Next: confirm the repeats.":counts().conflicts?"Next: resolve the conflicts.":`Next: <a href="${location.pathname.split("/toolbox/")[0]}/report/">build the report</a>.`}</div>`}
 h+=`<p class="t">${items.length} element(s)</p>`;
 let last=null;
 if(MODE==="repeats"){const G=[],idx=new Map();for(const x of items){const k=normT(txt(x))+"|"+x.rep.decision+"|"+(x.rep.reason||"");if(!idx.has(k)){idx.set(k,G.length);G.push({k,xs:[]})}G[idx.get(k)].xs.push(x)}GROUPS=G;
  if(G.some(g=>g.xs.length>1))h+=`<p class="t">Identical text is grouped: <b>Confirm all</b> applies the same decision to every copy; <b>Show each copy</b> to decide them one by one.</p>`;
  for(const g of G){const live=g.xs.filter(x=>{const key=x.fi+"|"+x.el.id;return openRow===key||full===key||edit?.el===key});
   if(g.xs.length>1&&!EXPAND.has(g.k)&&!live.length){h+=pairHTML(g.xs[0],g.xs[0].fi+"|"+g.xs[0].el.id,{n:g.xs.length,files:new Set(g.xs.map(x=>rk(x.d))).size,k:g.k});continue}
   if(g.xs.length>1)h+=`<div class="filehead">${g.xs.length} copies of the same text <button class="btn sm" data-expand="${esc(g.k)}">Group them again</button></div>`;
   for(const x of g.xs){const key=x.fi+"|"+x.el.id;h+=(openRow===key||full===key||edit?.el===key)?cardHTML(x.d,x.el,key,who):pairHTML(x,key,null)}}}
 else for(const x of items.slice(0,300)){const key=x.fi+"|"+x.el.id;
  if(rk(x.d)!==last){h+=`<div class="filehead">${x.d.fname?esc(x.d.fname)+" › ":""}${esc(x.d.name.replace(/\.txt$/,"").replace(/_/g," "))}${x.d.item&&x.d.item.score!=null?` · item score ${x.d.item.score}`:""}${MODE!=="done"?wiControls(x.d,x.fi):""}${x.d.item&&x.d.item.reason?`<div class="itemwhy">Model: ${esc(x.d.item.reason)}</div>`:""}</div>`;last=rk(x.d)}

  if(MODE==="done"&&openRow!==key&&focus!==key){h+=row(x.d,x.el).replace(`data-row="${x.el.id}"`,`data-row="${key}"`).replace("</div>",`${x.dec.copied_from?`<span class="t">copied from ${esc(x.dec.copied_from.split("#")[0])}</span>`:""}</div>`);continue}
  h+=cardHTML(x.d,x.el,key,who)}
 if(items.length>300)h+=`<p class="t">Showing the first 300; filter to see the rest.</p>`;
 if(!items.length)h+=`<p class="alldone">${MODE==="review"?"Nothing left to review at 7+.":MODE==="repeats"?"No repeats waiting.":"Nothing here."}</p>`;
 m.innerHTML=h;
 const qi=document.getElementById("q");qi.oninput=()=>{q=qi.value;const p=qi.selectionStart;render();const n=document.getElementById("q");n.focus();n.setSelectionRange(p,p)};
 m.querySelectorAll("[data-chip]").forEach(b=>b.onclick=()=>{for(const n of b.dataset.chip.split(",")){const s=F[n];if(b.dataset.v==="*")s.clear();else if(n===b.dataset.chip)s.has(b.dataset.v)?s.delete(b.dataset.v):s.add(b.dataset.v)}render()});
 const rs=document.getElementById("rsel");if(rs)rs.onchange=()=>{F.rr=rs.value;render()};const ss=document.getElementById("ssel");if(ss)ss.onchange=()=>{F.sb=ss.value;render()};
 m.querySelectorAll("[data-expand]").forEach(b=>b.onclick=()=>{const k=b.dataset.expand;EXPAND.has(k)?EXPAND.delete(k):EXPAND.add(k);render()});
 m.querySelectorAll("[data-rep]").forEach(b=>b.onclick=async()=>{
  if(b.dataset.rep==="okall"||b.dataset.rep==="noall"){const g=GROUPS.find(g=>g.k===b.dataset.gk);if(!g)return;
   if(b.dataset.rep==="noall"){g.xs.forEach(x=>DISMISS.add(x.fi+"|"+x.el.id));render();return}
   b.disabled=true;await applyRepeats(g.xs.map(x=>({fid:x.d.fid,file:x.d.name,element:x.el.id})));return}
  const key=b.closest(".pair").dataset.key,[fi,id]=splitKey(key);
  if(b.dataset.rep==="no"){DISMISS.add(key);render();return}
  await applyRepeats([{fid:DOCS[fi].fid,file:DOCS[fi].name,element:id}])});
 const ca=document.getElementById("confirmall");if(ca)ca.onclick=async()=>{ca.disabled=true;await applyRepeats(items.map(x=>({fid:x.d.fid,file:x.d.name,element:x.el.id})))};
 const gf=document.getElementById("gofirst");if(gf)gf.onclick=()=>{const first=m.querySelector(".el");if(first)goTo(first.dataset.el)};
 wire(m);nav()}
async function applyRepeats(only){const by={};for(const o of only){(by[o.fid||""]=by[o.fid||""]||[]).push({file:o.file,element:o.element})}let n=0,bad=0;
 for(const fid in by){const r=await fetch((fid&&fid!==PAGE_FID?"../"+fid+"/":"")+"api/apply-repeats",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify({only:by[fid]})});const j=await r.json();if(!r.ok){toast(j.error||"Not applied");return}n+=j.applied;bad+=j.failed}
 toast(`Recorded ${n} decision(s)${bad?`, ${bad} need deciding by hand`:""}`);await load(true);nav()}

// ---- Context: the whole document, the whole item and the conversation ---------------------------
let CTX=null;
function ctxChunks(list,label){if(!list||!list.length)return `<p class="t">Nothing more to show for this ${label}.</p>`;
 return list.map(c=>`<div class="ctxc ${c.this?"here":""}" ${c.this?'id="ctxhere"':""}><div class="ctxm">${c.this?"<b>▶ This one</b> · ":""}${esc((c.date||"").slice(0,16).replace("T"," "))}${c.from?" · From: "+esc(c.from):""}${c.to?" · To: "+esc(c.to):""}${c.subject?" · <b>"+esc(c.subject)+"</b>":""}${c.score!=null?` · model ${c.score}`:""}${c.about_subject?"":" · <i>does not name the subject</i>"}</div><div class="ctxt">${esc(c.content)}</div></div>`).join("")}
function ctxRender(tab){const j=CTX;if(!j)return;const b=document.getElementById("ctxbody");const db=j.db;
 document.querySelectorAll("#ctx [data-tab]").forEach(x=>x.classList.toggle("on",x.dataset.tab===tab));
 if(tab==="doc"){let h="",pos=0;const t=j.text;for(const [a,z] of j.parts){h+=esc(t.slice(pos,a))+`<mark ${pos===0||!h.includes("ctxhere")?'id="ctxhere"':""}>${esc(t.slice(a,z))}</mark>`;pos=z}h+=esc(t.slice(pos));b.innerHTML=`<div class="t">${esc(j.file)}</div><div class="ctxt">${h}</div>`}
 else if(tab==="item")b.innerHTML=db?`<div class="t">The original ${esc(db.source_type.replace("_"," "))} from the search: ${esc((db.source_path||"").split("/").pop())}</div>${ctxChunks(db.item,"item")}`:`<p class="t">This document is not linked to the case database, so only the document itself is available.</p>`;
 else b.innerHTML=db?(db.conversation.length?`<div class="t">${db.conversation.length} message(s) in this ${db.source_type==="chat_message"?"chat":"email thread"}, oldest first</div>${ctxChunks(db.conversation,"conversation")}`:`<p class="t">No conversation: this item has no thread.</p>`):`<p class="t">Not linked to the case database.</p>`;
 const here=document.getElementById("ctxhere");if(here)here.scrollIntoView({block:"center"})}
async function showContext(d,elId){if(!d)return;let box=document.getElementById("ctx");
 if(!box){box=document.createElement("div");box.id="ctx";box.className="ctx";document.body.appendChild(box);box.onclick=ev=>{if(ev.target===box)closeContext()}}
 box.innerHTML=`<div class="ctxbox"><div class="ctxhead"><b>Context</b><span class="toggle"><button data-tab="doc">This document</button><button data-tab="item">Whole item</button><button data-tab="conv">Conversation</button></span><span class="sp"></span><button class="btn" id="ctxclose">Close (Esc)</button></div><div class="ctxbody" id="ctxbody"><p class="t">Loading…</p></div></div>`;
 box.hidden=false;document.getElementById("ctxclose").onclick=closeContext;
 const r=await fetch(fbase(d)+"api/context?token="+TOKEN+"&file="+encodeURIComponent(d.name)+"&el="+encodeURIComponent(elId));CTX=await r.json();
 const db=CTX.db;box.querySelector('[data-tab="item"]').textContent=`Whole item${db?" ("+db.item.length+")":""}`;box.querySelector('[data-tab="conv"]').textContent=`Conversation${db?" ("+db.conversation.length+")":""}`;
 box.querySelectorAll("[data-tab]").forEach(x=>x.onclick=()=>ctxRender(x.dataset.tab));ctxRender("doc")}
function closeContext(){const b=document.getElementById("ctx");if(b)b.hidden=true;CTX=null}
function ctxFor(spec){const [fid,file,el]=spec.split("|");let i=di(fid,file);if(i<0)i=DOCS.findIndex(d=>d.name===file&&(!fid||(d.folder||"").endsWith(fid)));if(i<0)i=DOCS.findIndex(d=>d.name===file);if(i>=0)showContext(DOCS[i],el)}

// ---- Whole-item decisions ---------------------------------------------------------------------
const NOT_REQ="Not the requester's personal data";
function undecidedOf(d){return d.elements.filter(e=>!d.decisions[e.id]&&!e.blank)}
function wiControls(d,fi){const n=undecidedOf(d).length;if(!n)return "";
 return `<span class="wi" data-wfi="${fi}"><span class="t">Whole item (${n} undecided):</span><button class="btn sm" data-wi="release">Extract whole item</button><select class="btn sm" data-wr>${REASONS.map(x=>`<option ${x===NOT_REQ?"selected":""}>${esc(x)}</option>`).join("")}</select><button class="btn sm stop" data-wi="redact_full">Leave out whole item</button></span>`}
async function decideFile(fi,decision,reason){const d=DOCS[fi],n=undecidedOf(d).length;
 if(!confirm(decision==="release"?`Extract all ${n} undecided paragraph(s) of this item into the report?`:`Leave out all ${n} undecided paragraph(s) of this item as “${reason}”?`))return;
 const r=await fetch(fbase(d)+"api/decide-file",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify({file:d.name,decision,reason})});
 const j=await r.json();if(!r.ok){toast(j.error||"Not saved");return}toast(`Recorded ${j.decided} decision(s)`);await load(true);nav()}
function wireWI(m){m.querySelectorAll("[data-wi]").forEach(b=>b.onclick=()=>{const w=b.closest("[data-wfi]");decideFile(+w.dataset.wfi,b.dataset.wi,w.querySelector("[data-wr]").value)})}

function prio(el,dec){if(dec)return "";const s=el.score&&el.score.score;return s==null?"p-un":s>=9?"p-hi":s>=8?"p-mid":s>=7?"p-low":""}
function light(k,n){if(!n)return `<span class="lt ok">✓</span>`;return `<span class="lt ${k==="repeats"?"amb":k==="low"||k==="done"?"":"red"}">${n}</span>`}
function visibleIds(){return [...document.querySelectorAll("#main .el")].map(x=>x.dataset.el)}
function goTo(id){if(!id)return;focus=id;document.querySelectorAll(".el.focus").forEach(y=>y.classList.remove("focus"));const x=document.querySelector(`.el[data-el="${id}"]`);if(x){x.classList.add("focus");x.scrollIntoView({block:"center",behavior:"smooth"})}}
// After a decision, move to the next element not yet reviewed (in the current view and filter).
function advance(id){openRow=null;render();const ids=visibleIds(),i=ids.indexOf(id);const next=ids.slice(i+1).find(undecidedKey)||ids.find(undecidedKey);if(next)goTo(next);else toast("Every element shown here is reviewed")}
document.addEventListener("keydown",ev=>{if(ev.metaKey||ev.ctrlKey||ev.altKey)return;const t=ev.target;
 if(CTX||!(document.getElementById("ctx")||{hidden:true}).hidden){if(ev.key==="Escape"){closeContext();ev.preventDefault()}return}
 if(ev.key==="Escape"){if(full){full=null;render()}else if(split){split=null;render()}else if(edit&&(!edit.marks?.length||confirm("Discard unsaved marks?"))){edit=null;render()}return}
 if(t.closest&&t.closest("input,select,textarea"))return;if(edit||split||full)return;
 const ids=visibleIds();if(!ids.length)return;let i=ids.indexOf(focus);
 const k=ev.key.toLowerCase();
 if(k==="arrowdown"||k==="j"){ev.preventDefault();goTo(ids[Math.min(i+1,ids.length-1)]||ids[0])}
 else if(k==="arrowup"||k==="k"){ev.preventDefault();goTo(ids[Math.max(i-1,0)])}
 else if(["a","e","r","l","p"].includes(k)){if(i<0){goTo(ids.find(undecidedKey)||ids[0]);return}ev.preventDefault();
  const b=document.querySelector(`.el[data-el="${focus}"] [data-a="${{a:"approve",e:"approve",r:"full",l:"full",p:"partial"}[k]}"]`);if(b)b.click()}});
function offset(node,off){const el=(node.nodeType===3?node.parentElement:node).closest("[data-s]");if(!el)return null;let n=0;const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let t;while((t=w.nextNode())){if(t===node)return +el.dataset.s+n+off;n+=t.textContent.length}return +el.dataset.s+(node.nodeType===3?off:n)}
document.addEventListener("click",ev=>{if(!split)return;const box=document.querySelector(`.el[data-el="${split.el}"] .txt`);if(!box||!box.contains(ev.target))return;
 let node,off;if(document.caretRangeFromPoint){const r=document.caretRangeFromPoint(ev.clientX,ev.clientY);if(!r)return;node=r.startContainer;off=r.startOffset}else if(document.caretPositionFromPoint){const p=document.caretPositionFromPoint(ev.clientX,ev.clientY);if(!p)return;node=p.offsetNode;off=p.offset}else return;
 const at=offset(node,off);if(at==null)return;const sw=document.getElementById("sw");split.who=sw?sw.value:split.who;split.at=at;render()});
document.addEventListener("mouseup",()=>{if(!edit)return;const sel=getSelection();if(!sel.rangeCount||sel.isCollapsed)return;const r=sel.getRangeAt(0);
 const box=document.querySelector(`.el[data-el="${edit.el}"] .txt`);if(!box||!box.contains(r.startContainer)||!box.contains(r.endContainer)){toast("Select text inside the element you are marking");return}
 let a=offset(r.startContainer,r.startOffset),b=offset(r.endContainer,r.endOffset);if(a==null||b==null)return;if(b<a)[a,b]=[b,a];
 const [efi,eid]=splitKey(edit.el),d=DOCS[efi],el=d.elements.find(e=>e.id===eid),reason=document.getElementById("er").value,note=document.getElementById("en").value;
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
