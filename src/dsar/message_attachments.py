"""
Attachments inside saved emails (.eml / .msg) found among the exported files.

Saved emails turn up on SharePoint/OneDrive and as PST attachments. Their
chunkers read the header and body only, so without this step an attachment
inside a saved email is never classified. `dsar attachments` writes each one
out under input/PST_attachments/_messages/, where ingest picks it up with the
ordinary file chunkers, and catalogues it against its parent file.

Attachments that are themselves emails are opened in turn, to MAX_DEPTH.
Every instance is catalogued; stored files are never overwritten.
"""
import csv
import hashlib
import mimetypes
from email import policy
from email.parser import BytesParser
from pathlib import Path

from dsar.pst_attachments import ATTACH_DIR, _safe_name, _write_once

SUBDIR = "_messages"
CATALOGUE_NAME = "message_attachments_catalogue.csv"
MESSAGE_EXTS = {".eml", ".msg"}
MAX_DEPTH = 3

CATALOGUE_FIELDS = ["root_rel", "parent_rel", "depth", "attach_index", "original_name", "mime_type",
                    "size_bytes", "sha256", "stored_rel_path", "note"]


def _eml_attachments(path: Path) -> list[tuple[str, str, bytes | None, str]]:
    """(name, mime, bytes, note) per attachment of an .eml file."""
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    out = []
    for part in msg.iter_attachments():
        mime = part.get_content_type()
        name = part.get_filename() or ""
        try:
            if mime == "message/rfc822":
                inner = part.get_payload()[0] if part.is_multipart() else part.get_content()
                data = inner.as_bytes(policy=policy.default)
                name = name or f"{inner.get('Subject') or 'attached message'}.eml"
            else:
                data = part.get_payload(decode=True) or b""
            out.append((name, mime, data, ""))
        except Exception as exc:
            out.append((name, mime, None, f"unreadable attachment: {exc}"))
    return out


def _msg_attachments(path: Path) -> list[tuple[str, str, bytes | None, str]]:
    import extract_msg  # lazy import

    msg = extract_msg.openMsg(str(path))
    out = []
    try:
        for att in msg.attachments:
            name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None) or ""
            mime = getattr(att, "mimetype", None) or ""
            try:
                data = att.data
                if hasattr(data, "exportBytes"):          # an embedded Outlook message
                    name = name if name.lower().endswith(".msg") else f"{name or 'attached message'}.msg"
                    data = data.exportBytes()
                if not isinstance(data, (bytes, bytearray)):
                    out.append((name, mime, None, f"unsupported attachment type {type(data).__name__}"))
                    continue
                out.append((name, mime, bytes(data), ""))
            except Exception as exc:
                out.append((name, mime, None, f"unreadable attachment: {exc}"))
    finally:
        try:
            msg.close()
        except Exception:
            pass
    return out


def find_messages(input_dir: Path) -> list[Path]:
    """Saved emails among the exported files (not those this step itself wrote)."""
    out = []
    for p in sorted(input_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in MESSAGE_EXTS:
            continue
        parts = p.relative_to(input_dir).parts
        if parts[:2] == (ATTACH_DIR, SUBDIR):
            continue
        out.append(p)
    return out


def extract(message_paths: list[Path], input_dir: Path) -> list[dict]:
    out_root = input_dir / ATTACH_DIR / SUBDIR
    rows: list[dict] = []
    queue = [(p, p.relative_to(input_dir).as_posix(), 0) for p in message_paths]
    while queue:
        path, root_rel, depth = queue.pop(0)
        parent_rel = path.relative_to(input_dir).as_posix()
        reader = _eml_attachments if path.suffix.lower() == ".eml" else _msg_attachments
        try:
            attachments = reader(path)
        except Exception as exc:
            rows.append({"root_rel": root_rel, "parent_rel": parent_rel, "depth": depth,
                         "note": f"message unreadable: {exc}"})
            continue
        folder = out_root / Path(parent_rel).with_suffix("")
        for idx, (name, mime, data, note) in enumerate(attachments):
            row = {"root_rel": root_rel, "parent_rel": parent_rel, "depth": depth, "attach_index": idx,
                   "original_name": name, "mime_type": mime, "note": note}
            if data is None:
                rows.append(row)
                continue
            if "." not in name and mime:
                name = (name or "attachment") + (mimetypes.guess_extension(mime) or "")
            target = folder / f"{idx:02d}_{_safe_name(name or 'attachment')}"
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_once(target, data)
            row.update(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                       stored_rel_path=target.relative_to(input_dir).as_posix())
            rows.append(row)
            if target.suffix.lower() in MESSAGE_EXTS:
                if depth + 1 < MAX_DEPTH:
                    queue.append((target, root_rel, depth + 1))
                else:
                    row["note"] = f"nested message deeper than {MAX_DEPTH}: its attachments not extracted"
    return rows


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
