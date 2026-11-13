"""
Teams chat messages in a PST (Purview / compliance export).

How they appear:
  - each chat message is its own item, message class "IPM.SkypeTeams.Message";
  - the sender is PR_SENDER_NAME and the other participants are in PR_DISPLAY_TO
    (semicolon-separated display names);
  - the text is in the HTML body (the plain-text body is usually empty);
  - delivery_time is the message's timestamp.

A conversation is every message with the same Teams thread ID ("19:...@unq.gbl.spaces"
for a 1:1 chat, "19:...@thread.v2" for a group or meeting chat, "@thread.tacv2" /
"@thread.skype" for a channel). Purview puts it in property 0x366D; if that's
missing, any string property whose whole value is a thread ID is used. Messages
with no thread ID at all are grouped by their exact set of participants instead
(a group chat whose members changed then shows as one conversation per
membership). Participant names are matched case-insensitively.
"""
import hashlib
import html
import re
from html.parser import HTMLParser

from dsar.toolbox.mailbox import reader

TEAMS_CLASS_PREFIX = "IPM.SkypeTeams"
THREAD_ID_TAG = 0x366D
_THREAD = re.compile(r"19:[^\s;]+@(?:thread\.(?:v2|skype|tacv2)|unq\.gbl\.spaces)", re.IGNORECASE)
_PR_DISPLAY_TO = 0x0E04
_PR_SENDER_SMTP = 0x5D01
_PR_SENDER_EMAIL = 0x0C1F


class _Text(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            self.parts.append(f"[image{': ' + alt if alt else ''}]")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self.skip:
            self.skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\xa0]+", " ", t)
        t = re.sub(r" *\n *", "\n", t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    p = _Text()
    try:
        p.feed(raw)
        return p.text()
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", raw)).strip()


def body_text(msg) -> str:
    b = reader.bodies(msg)
    if b["html"].strip():
        return html_to_text(b["html"])
    return (b["plain"] or b["rtf_text"]).strip()


def norm(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def conversation_key(participants) -> str:
    keys = sorted({norm(p) for p in participants if norm(p)})
    return hashlib.sha256("\x1f".join(keys).encode()).hexdigest()[:12]


def thread_id(msg) -> str:
    """The Teams thread ID, or "" if the message carries none."""
    value = reader._record_string(msg, THREAD_ID_TAG)
    if _THREAD.fullmatch(value):
        return value
    try:
        for i in range(msg.number_of_record_sets):
            rs = msg.get_record_set(i)
            for j in range(rs.number_of_entries):
                entry = rs.get_entry(j)
                try:
                    s = reader.safe_str(entry.data_as_string).strip()
                except Exception:
                    continue
                if s and _THREAD.fullmatch(s):
                    return s
    except Exception:
        pass
    return ""


def thread_kind(thread: str, n_people: int) -> str:
    t = thread.lower()
    if t.endswith("@unq.gbl.spaces"):
        return "1:1"
    if t.endswith("@thread.tacv2") or t.endswith("@thread.skype"):
        return "channel"
    if t:
        return "group"
    return "1:1" if n_people == 2 else ("self" if n_people < 2 else "group")


def build_index(pff) -> list[dict]:
    """One entry per Teams message: where it is, its thread and who is in it (bodies are read later)."""
    out = []
    for ix, _folder, i, msg in reader.walk(pff):
        if not reader.message_class(msg).startswith(TEAMS_CLASS_PREFIX):
            continue
        try:
            sender = reader.safe_str(msg.sender_name).strip()
        except Exception:
            sender = ""
        rcpt = [p.strip() for p in reader._record_string(msg, _PR_DISPLAY_TO).split(";") if p.strip()]
        people = []
        for p in [sender] + rcpt:
            if p and norm(p) not in {norm(x) for x in people}:
                people.append(p)
        thread = thread_id(msg)
        key = hashlib.sha256(thread.encode()).hexdigest()[:12] if thread else conversation_key(people)
        out.append({"f": ix, "i": i, "sender": sender or "(unknown)",
                    "sender_email": reader._record_string(msg, _PR_SENDER_SMTP)
                    or reader._record_string(msg, _PR_SENDER_EMAIL),
                    "participants": people, "thread": thread, "key": key, "date": reader.message_date(msg)})
    return out


def _union(rows: list[dict]) -> list[str]:
    seen, out = set(), []
    for m in rows:
        for p in m["participants"]:
            if norm(p) not in seen:
                seen.add(norm(p))
                out.append(p)
    return out


def conversations(index: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for m in index:
        groups.setdefault(m["key"], []).append(m)
    out = []
    for key, rows in groups.items():
        dates = sorted(m["date"] for m in rows if m["date"])
        people = _union(rows)
        out.append({"key": key, "thread": rows[0]["thread"], "participants": people, "count": len(rows),
                    "first": dates[0] if dates else "", "last": dates[-1] if dates else "",
                    "kind": thread_kind(rows[0]["thread"], len(people))})
    out.sort(key=lambda c: c["last"] or "", reverse=True)
    return out


def participants(index: list[dict]) -> list[dict]:
    """People ranked by activity: {name, email, sent, total, partners (distinct 1:1 partners)}."""
    stats: dict[str, dict] = {}
    partners: dict[str, set] = {}
    for m in index:
        for p in m["participants"]:
            r = stats.setdefault(norm(p), {"name": p, "email": "", "sent": 0, "total": 0})
            r["total"] += 1
        s = stats.get(norm(m["sender"]))
        if s:
            s["sent"] += 1
            s["email"] = s["email"] or m["sender_email"]
        if len(m["participants"]) == 2:
            a, b = (norm(p) for p in m["participants"])
            partners.setdefault(a, set()).add(b)
            partners.setdefault(b, set()).add(a)
    out = [{**r, "partners": len(partners.get(k, ()))} for k, r in stats.items()]
    out.sort(key=lambda r: (-r["total"], r["name"].casefold()))
    return out


def messages(pff, index: list[dict], key: str) -> tuple[list[dict], dict]:
    """The conversation's messages in time order, with text, and its metadata."""
    rows = [m for m in index if m["key"] == key]
    out = []
    for m in rows:
        folder = reader.get_folder(pff, m["f"])
        try:
            text = body_text(folder.get_sub_message(m["i"]))
        except Exception as exc:  # noqa: BLE001 - shown in place of the text, never dropped
            text = f"[message could not be read: {exc}]"
        out.append({"sender": m["sender"], "sender_email": m["sender_email"], "date": m["date"], "text": text,
                    "f": m["f"], "i": m["i"]})
    out.sort(key=lambda x: (x["date"] or "", x["f"], x["i"]))
    people = _union(rows)
    meta = {"key": key, "thread": rows[0]["thread"] if rows else "", "participants": people, "count": len(out),
            "kind": thread_kind(rows[0]["thread"], len(people)) if rows else "",
            "first": out[0]["date"] if out else "", "last": out[-1]["date"] if out else ""}
    return out, meta


def find_between(index: list[dict], a: str, b: str) -> str | None:
    """The key of the 1:1 conversation between two named people, if there is one."""
    want = {norm(a), norm(b)}
    if len(want) != 2:
        return None
    for c in conversations(index):
        if c["kind"] == "1:1" and {norm(p) for p in c["participants"]} == want:
            return c["key"]
    return None
