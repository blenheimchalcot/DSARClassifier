"""
PST attachment extraction.

Purview PST exports keep attachments inside the PST ("Items extracted from
parent items: 0") and the PST chunker reads message bodies only, so without
this step no email attachment is ever classified. `dsar attachments` writes
each attachment out under input/PST_attachments/, where ingest picks it up with
the ordinary file chunkers, exactly like a SharePoint/OneDrive file.

Every attachment *instance* is catalogued against its parent email (PST,
message identifier, Message-ID, subject, sender, sent time). Content is stored
once per SHA-256: a letter forwarded five times is classified once and traced
back to all five emails through the catalogue.

Embedded items (attach method 5, e.g. recurring-meeting exceptions or an email
attached to an email) can't be opened as messages through pypff. Their raw
bytes are kept as `.bin` and their readable text is recovered into a sibling
`.txt` for classification.
"""
import csv
import hashlib
import mimetypes
import re
from pathlib import Path

ATTACH_DIR = "PST_attachments"
CATALOGUE_NAME = "pst_attachments_catalogue.csv"

# MAPI property tags read from each attachment's record set.
_PR_DISPLAY_NAME = 0x3001
_PR_ATTACH_FILENAME = 0x3704
_PR_ATTACH_METHOD = 0x3705
_PR_ATTACH_LONG_FILENAME = 0x3707
_PR_ATTACH_MIME_TAG = 0x370E
_PR_ATTACH_CONTENT_ID = 0x3712
_INT_TAGS = {_PR_ATTACH_METHOD}
_WANTED = {_PR_DISPLAY_NAME, _PR_ATTACH_FILENAME, _PR_ATTACH_METHOD,
           _PR_ATTACH_LONG_FILENAME, _PR_ATTACH_MIME_TAG, _PR_ATTACH_CONTENT_ID}

_METHOD_EMBEDDED = 5
_HEADER_MESSAGE_ID = re.compile(r"^Message-ID:\s*(<[^>\r\n]+>)", re.MULTILINE | re.IGNORECASE)
_ITEM_CLASS = re.compile(r"IPM\.[A-Za-z0-9_.{}\-]+")
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

CATALOGUE_FIELDS = [
    "pst_rel", "parent_identifier", "parent_message_id", "parent_subject", "parent_sender",
    "parent_sent", "attach_index", "attach_method", "original_name", "mime_type", "content_id",
    "embedded_class", "size_bytes", "sha256", "stored_rel_path", "recovered_text_rel_path",
    "first_instance", "note",
]


def _safe_str(value) -> str:
    if value is None:
        return ""
    s = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _props(attachment) -> dict[int, object]:
    out: dict[int, object] = {}
    for record_set in attachment.record_sets:
        for entry in record_set.entries:
            tag = entry.entry_type
            if tag not in _WANTED or tag in out:
                continue
            try:
                out[tag] = entry.get_data_as_integer() if tag in _INT_TAGS else _safe_str(entry.get_data_as_string())
            except Exception:
                out[tag] = None
    return out


def _safe_name(name: str, limit: int = 120) -> str:
    name = _UNSAFE.sub("_", name).strip(" .") or "attachment"
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    return (stem[: limit - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:limit]


def recover_text(buf: bytes) -> str:
    """Readable text from an opaque item blob: UTF-16LE runs (MAPI strings) plus ASCII runs."""
    lines: list[str] = []
    for m in re.finditer(rb"(?:[\x20-\x7e\xa0-\xff][\x00]){4,}|(?:[\t\r\n][\x00])", buf):
        lines.append(m.group(0).decode("utf-16-le", errors="ignore"))
    utf16 = "".join(lines)
    ascii_runs = [m.group(0).decode("latin-1") for m in re.finditer(rb"[\x20-\x7e\t\r\n]{6,}", buf)]
    out: list[str] = []
    for line in re.split(r"[\r\n]+", utf16) + ascii_runs:
        line = line.strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


def _iter_messages(folder):
    for i in range(folder.number_of_sub_messages):
        try:
            yield folder.get_sub_message(i)
        except Exception:
            continue
    for i in range(folder.number_of_sub_folders):
        try:
            sub = folder.get_sub_folder(i)
        except Exception:
            continue
        yield from _iter_messages(sub)


def _parent_fields(message) -> dict[str, str]:
    def get(attr):
        try:
            return getattr(message, attr, None)
        except Exception:
            return None
    headers = _safe_str(get("transport_headers"))
    m = _HEADER_MESSAGE_ID.search(headers)
    sent = get("client_submit_time")
    return {
        "parent_identifier": str(get("identifier") or ""),
        "parent_message_id": m.group(1) if m else "",
        "parent_subject": _safe_str(get("subject")),
        "parent_sender": _safe_str(get("sender_name")),
        "parent_sent": sent.isoformat() if sent else "",
    }


def extract(pst_paths: list[Path], input_dir: Path) -> list[dict]:
    """Write attachments under input/PST_attachments/ and return one catalogue row per instance."""
    import pypff  # lazy import

    out_root = input_dir / ATTACH_DIR
    stored_by_sha: dict[str, tuple[str, str]] = {}
    rows: list[dict] = []

    for pst_path in pst_paths:
        pst_rel = pst_path.relative_to(input_dir).as_posix()
        pff = pypff.file()
        pff.open(str(pst_path))
        try:
            for message in _iter_messages(pff.get_root_folder()):
                try:
                    n = message.number_of_attachments
                except Exception:
                    continue
                if not n:
                    continue
                parent = _parent_fields(message)
                for idx in range(n):
                    row = {"pst_rel": pst_rel, **parent, "attach_index": idx, "note": ""}
                    try:
                        att = message.get_attachment(idx)
                        p = _props(att)
                        size = att.get_size()
                        data = att.read_buffer(size) if size else b""
                    except Exception as exc:
                        row.update(note=f"unreadable attachment: {exc}", first_instance="")
                        rows.append(row)
                        continue

                    method = p.get(_PR_ATTACH_METHOD)
                    name = (p.get(_PR_ATTACH_LONG_FILENAME) or p.get(_PR_ATTACH_FILENAME)
                            or p.get(_PR_DISPLAY_NAME) or "")
                    mime = p.get(_PR_ATTACH_MIME_TAG) or ""
                    embedded_class = ""
                    if method == _METHOD_EMBEDDED:
                        head = data[:4000].decode("utf-16-le", errors="ignore")
                        cls = _ITEM_CLASS.search(head)
                        embedded_class = cls.group(0) if cls else ""
                    digest = hashlib.sha256(data).hexdigest()
                    row.update(attach_method=method, original_name=name, mime_type=mime,
                               content_id=p.get(_PR_ATTACH_CONTENT_ID) or "",
                               embedded_class=embedded_class, size_bytes=len(data), sha256=digest)

                    if digest in stored_by_sha:
                        stored, recovered = stored_by_sha[digest]
                        row.update(stored_rel_path=stored, recovered_text_rel_path=recovered,
                                   first_instance="no")
                        rows.append(row)
                        continue

                    folder = out_root / pst_path.stem / (parent["parent_identifier"] or "unknown")
                    folder.mkdir(parents=True, exist_ok=True)
                    recovered_rel = ""
                    if method == _METHOD_EMBEDDED:
                        base = f"{idx:02d}_embedded_{_safe_name(name or 'item', 60)}"
                        target = folder / f"{base}.bin"
                        text_target = folder / f"{base}.txt"
                        text = recover_text(data)
                        header = (f"[Text recovered from an embedded PST item ({embedded_class or 'unknown class'}) "
                                  f"attached to email '{parent['parent_subject']}' sent {parent['parent_sent']}. "
                                  f"Raw item preserved alongside as .bin.]\n\n")
                        _write_once(text_target, (header + text).encode("utf-8"))
                        recovered_rel = text_target.relative_to(input_dir).as_posix()
                        if embedded_class == "IPM.Note":
                            # The embedded message's body lives in a sub-node pypff can't reach;
                            # only its property block (headers, recipients, subject) is recovered.
                            row["note"] = ("embedded email: headers/subject recovered, body NOT recovered "
                                           "(pypff limitation) - manual review or readpst needed")
                    else:
                        # Inline images are often named by bare content-ID; give the stored
                        # file its type's extension so ingest dispatches it correctly.
                        if not name:
                            name = "attachment"
                        if "." not in name.rsplit("/", 1)[-1] and mime:
                            name += mimetypes.guess_extension(mime) or ""
                        target = folder / f"{idx:02d}_{_safe_name(name)}"
                    _write_once(target, data)
                    stored_rel = target.relative_to(input_dir).as_posix()
                    stored_by_sha[digest] = (stored_rel, recovered_rel)
                    row.update(stored_rel_path=stored_rel, recovered_text_rel_path=recovered_rel,
                               first_instance="yes")
                    rows.append(row)
        finally:
            pff.close()
    return rows


def _write_once(path: Path, data: bytes) -> None:
    """Idempotent re-runs; never silently overwrite different content."""
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError(f"refusing to overwrite {path} with different content")
        return
    path.write_bytes(data)


def write_catalogue(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CATALOGUE_FIELDS, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CATALOGUE_FIELDS})


def load_catalogue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
