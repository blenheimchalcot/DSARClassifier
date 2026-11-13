"""
Read a PST (pypff) or MBOX (dsar.toolbox.mailbox.mbox) for display.

Folders are addressed by their index path from the root ("" is the root, "0.3"
is the root's first sub-folder's fourth sub-folder), so two folders with the
same name can never be confused. Messages are addressed by (folder, index).

Nothing here writes to the mailbox or to disk.
"""
import re
from email import policy
from email.message import EmailMessage

from dsar.chunking import pst as pst_chunker
from dsar.toolbox.mailbox import mbox

_PR_DISPLAY_TO = 0x0E04
_PR_DISPLAY_CC = 0x0E03
_PR_DISPLAY_BCC = 0x0E02
_PR_MESSAGE_CLASS = 0x001A
_PR_ATTACH_LONG_FILENAME = 0x3707
_PR_DISPLAY_NAME = 0x3001
_CHARSET = re.compile(rb"charset=[\"']?([\w\-]+)", re.IGNORECASE)


def open_mailbox(path: str):
    """Open a PST or MBOX read-only; returns a pypff.file-like object."""
    if mbox.is_mbox_path(path):
        return mbox.open_mbox(path)
    import pypff  # lazy: tests and MBOX use don't need it

    f = pypff.file()
    f.open(str(path), "rb")
    return f


def is_mbox(path: str) -> bool:
    return mbox.is_mbox_path(path)


safe_str = pst_chunker._safe_str


def format_date(value) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def message_date(msg) -> str:
    """Delivery time, else submit time (sent items and drafts have no delivery time)."""
    for attr in ("delivery_time", "client_submit_time"):
        try:
            value = getattr(msg, attr, None)
        except Exception:
            value = None
        if value:
            return format_date(value)
    return ""


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

def folder_tree(folder, ix: str = "") -> dict:
    """{name, ix, count, children} for the folder and everything under it."""
    children = []
    try:
        for i in range(folder.number_of_sub_folders):
            children.append(folder_tree(folder.get_sub_folder(i), f"{ix}.{i}" if ix else str(i)))
    except Exception:
        pass
    try:
        count = folder.number_of_sub_messages
    except Exception:
        count = 0
    return {"name": safe_str(folder.name) or "(unnamed)", "ix": ix, "count": count, "children": children}


def get_folder(pff, ix: str):
    """The folder at index path `ix`, or None."""
    folder = pff.get_root_folder()
    if not ix:
        return folder
    try:
        for part in ix.split("."):
            n = int(part)
            if not 0 <= n < folder.number_of_sub_folders:
                return None
            folder = folder.get_sub_folder(n)
    except (ValueError, OSError, IndexError):
        return None
    return folder


def folder_name_path(pff, ix: str) -> str:
    """Human-readable path ("Top of Information Store/Inbox") for a folder index path."""
    names = []
    folder = pff.get_root_folder()
    for part in (ix.split(".") if ix else []):
        try:
            folder = folder.get_sub_folder(int(part))
        except Exception:
            break
        names.append(safe_str(folder.name) or "(unnamed)")
    return "/".join(names) or "(root)"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _record_string(item, tag: int) -> str:
    try:
        for i in range(item.number_of_record_sets):
            rs = item.get_record_set(i)
            for j in range(rs.number_of_entries):
                entry = rs.get_entry(j)
                if entry.entry_type == tag:
                    try:
                        value = entry.data_as_string
                    except Exception:
                        continue
                    if value:
                        return safe_str(value).strip()
    except Exception:
        pass
    return ""


def message_class(msg) -> str:
    return _record_string(msg, _PR_MESSAGE_CLASS)


def transport_header(msg, name: str) -> str:
    try:
        headers = safe_str(msg.transport_headers)
    except Exception:
        return ""
    for line in headers.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line[len(name) + 1:].strip()
    return ""


def sender_email(msg) -> str:
    sender = transport_header(msg, "From")
    if "<" in sender and ">" in sender:
        return sender[sender.index("<") + 1: sender.index(">")].strip()
    return sender.strip()


def sender_label(msg) -> str:
    try:
        name = safe_str(msg.sender_name)
    except Exception:
        name = ""
    email = sender_email(msg)
    if name and email and name != email:
        return f"{name} <{email}>"
    return name or email


def recipients(msg) -> dict:
    """To / Cc / Bcc from the transport headers, else from the MAPI display properties.

    Sent items, drafts and most Purview-exported items carry no transport headers,
    so without the display properties their recipients would show as blank.
    """
    display = pst_chunker._display_recipients(msg)
    return {
        "to": transport_header(msg, "To") or display.get(_PR_DISPLAY_TO, ""),
        "cc": transport_header(msg, "Cc") or display.get(_PR_DISPLAY_CC, ""),
        "bcc": display.get(_PR_DISPLAY_BCC, ""),
    }


def decode_html(raw) -> str:
    """Decode HTML bytes honouring a declared charset (Teams items often declare windows-1252)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    encodings = []
    m = _CHARSET.search(raw[:2048])
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()
        if declared not in ("us-ascii", "ascii"):
            encodings.append(declared)
    for enc in encodings + ["utf-8", "cp1252", "latin-1"]:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def bodies(msg) -> dict:
    """{html, plain, rtf_text}: each read independently, as corrupt records raise on access."""
    out = {"html": "", "plain": "", "rtf_text": ""}
    try:
        out["html"] = decode_html(msg.html_body)
    except Exception:
        pass
    try:
        raw = msg.plain_text_body
        out["plain"] = safe_str(raw) if raw else ""
    except Exception:
        pass
    if not out["html"].strip() and not out["plain"].strip():
        rtf = pst_chunker._read_body_attr(msg, "rtf_body")
        if rtf.strip():
            try:
                from striprtf.striprtf import rtf_to_text
                out["rtf_text"] = safe_str(rtf_to_text(rtf, errors="ignore").strip())
            except Exception:
                pass
    return out


def summary(msg, index: int) -> dict:
    try:
        subject = safe_str(msg.subject)
    except Exception:
        subject = ""
    try:
        n_att = msg.number_of_attachments
    except Exception:
        n_att = 0
    return {"index": index, "subject": subject, "sender": sender_label(msg), "date": message_date(msg),
            "attachments": n_att}


def list_messages(folder, page: int = 1, per_page: int = 50) -> dict:
    try:
        total = folder.number_of_sub_messages
    except Exception:
        total = 0
    start = (page - 1) * per_page
    messages = []
    for i in range(start, min(start + per_page, total)):
        try:
            messages.append(summary(folder.get_sub_message(i), i))
        except Exception:
            messages.append({"index": i, "subject": "(unreadable message)", "sender": "", "date": "",
                             "attachments": 0})
    return {"total": total, "page": page, "per_page": per_page, "messages": messages}


def attachment_name(att, index: int = 0) -> str:
    if isinstance(att, mbox.MboxAttachment):
        name = att.filename
    else:
        name = _record_string(att, _PR_ATTACH_LONG_FILENAME) or _record_string(att, _PR_DISPLAY_NAME)
    return name or f"attachment_{index + 1}"


def message_detail(folder, index: int) -> dict:
    msg = folder.get_sub_message(index)
    attachments = []
    try:
        for i in range(msg.number_of_attachments):
            att = msg.get_attachment(i)
            try:
                size = att.size
            except Exception:
                size = 0
            attachments.append({"name": attachment_name(att, i), "size": size})
    except Exception:
        pass
    body = bodies(msg)
    return {
        "index": index,
        "subject": summary(msg, index)["subject"],
        "sender": sender_label(msg),
        **recipients(msg),
        "date": message_date(msg),
        "message_class": message_class(msg),
        "html_body": body["html"],
        "plain_body": body["plain"] or body["rtf_text"],
        "attachments": attachments,
    }


def walk(pff):
    """Yield (folder_ix, folder, message_index, message) for every message in the mailbox."""
    stack = [("", pff.get_root_folder())]
    while stack:
        ix, folder = stack.pop()
        try:
            n = folder.number_of_sub_messages
        except Exception:
            n = 0
        for i in range(n):
            try:
                yield ix, folder, i, folder.get_sub_message(i)
            except Exception:
                continue
        try:
            subs = [(f"{ix}.{j}" if ix else str(j), folder.get_sub_folder(j))
                    for j in range(folder.number_of_sub_folders)]
        except Exception:
            subs = []
        stack.extend(reversed(subs))


def search(pff, query: str, limit: int = 200) -> list[dict]:
    """Messages whose subject, sender or recipients contain `query` (case-insensitive)."""
    q = query.casefold().strip()
    out = []
    if not q:
        return out
    for ix, _folder, i, msg in walk(pff):
        try:
            subject = safe_str(msg.subject)
        except Exception:
            subject = ""
        sender = sender_label(msg)
        rcpt = recipients(msg)
        haystack = " ".join([subject, sender, rcpt["to"], rcpt["cc"], rcpt["bcc"]]).casefold()
        if q in haystack:
            out.append({"folder": ix, "folder_name": folder_name_path(pff, ix), "index": i, "subject": subject,
                        "sender": sender, "date": message_date(msg)})
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def attachment_data(folder, msg_index: int, att_index: int) -> tuple[str, bytes]:
    """(filename, bytes). An attached email has no byte stream, so it is rebuilt as .eml."""
    msg = folder.get_sub_message(msg_index)
    name, data, is_message = _read_attachment(msg, att_index)
    if is_message and not name.lower().endswith(".eml"):
        name += ".eml"
    return name, data


def _read_attachment(msg, att_index: int) -> tuple[str, bytes, bool]:
    att = msg.get_attachment(att_index)
    name = attachment_name(att, att_index)
    if isinstance(att, mbox.MboxAttachment):
        return name, att.data, att.is_message
    data = b""
    try:
        size = att.size
        if size:
            att.seek_offset(0)
            data = att.read_buffer(size)
    except Exception:
        data = b""
    if data:
        return name, data, False
    try:
        if att.number_of_sub_items > 0:
            return name, message_to_eml(att.get_sub_item(0)), True
    except Exception:
        pass
    raise ValueError("this attachment has no readable content")


def message_to_eml(msg) -> bytes:
    """Rebuild a message as RFC 822 bytes (headers, bodies, attachments)."""
    em = EmailMessage(policy=policy.default)
    rcpt = recipients(msg)
    try:
        subject = safe_str(msg.subject)
    except Exception:
        subject = ""
    for header, value in (("Subject", subject), ("From", sender_label(msg)), ("To", rcpt["to"]),
                          ("Cc", rcpt["cc"]), ("Date", transport_header(msg, "Date"))):
        if value:
            try:
                em[header] = value.replace("\r", " ").replace("\n", " ")
            except Exception:
                pass
    body = bodies(msg)
    em.set_content(body["plain"] or body["rtf_text"] or "(no text body)")
    if body["html"]:
        em.add_alternative(body["html"], subtype="html")
    try:
        for i in range(msg.number_of_attachments):
            try:
                sub_name, sub_data, sub_is_msg = _read_attachment(msg, i)
                if sub_is_msg and not sub_name.lower().endswith(".eml"):
                    sub_name += ".eml"
                em.add_attachment(sub_data, maintype="application", subtype="octet-stream", filename=sub_name)
            except Exception:
                pass
    except Exception:
        pass
    return em.as_bytes()
