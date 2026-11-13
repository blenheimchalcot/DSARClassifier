"""
MBOX support, exposed through pypff-shaped adapter objects (ported from PSTViewer).

The mailbox reader (dsar.toolbox.mailbox.reader) is written against pypff's
file / folder / message / attachment interface. Rather than fork every code
path, this module wraps Python's stdlib `mailbox` so an MBOX looks like a PST:
a root folder containing one folder per mailbox.

Supported inputs:
  - A single .mbox file (Gmail / Google Takeout, Thunderbird, generic Unix mbox)
  - An Apple Mail export: a "Name.mbox" directory containing an "mbox" file
  - A directory tree of the above — each mailbox becomes a folder

Uses only the Python standard library. All processing is local-only.
"""

import os
import email
import mailbox
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

# Headers surfaced through the pypff-style `transport_headers` attribute.
_HEADER_NAMES = ("From", "To", "Cc", "Subject", "Date")


def is_mbox_path(path: str) -> bool:
    """True if `path` looks like something open_mbox() can read."""
    if os.path.isdir(path):
        return _find_mailboxes(path) != []
    base = os.path.basename(path).lower()
    return base.endswith(".mbox") or base == "mbox"


def open_mbox(path: str) -> "MboxFile":
    """Open an MBOX file or directory and return a pypff.file-like object."""
    return MboxFile(path)


def _find_mailboxes(path: str):
    """
    Return [(folder_segments, mbox_file_path), ...] for every mailbox under a
    directory. An Apple Mail "X.mbox" bundle contributes its inner "mbox" file.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        rel = os.path.relpath(dirpath, path)
        segments = [] if rel == "." else rel.split(os.sep)
        # Strip ".mbox" from bundle directory names for display
        segments = [s[:-5] if s.lower().endswith(".mbox") else s for s in segments]
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if fn == "mbox":
                found.append((segments or [_display_name(path)], full))
            elif fn.lower().endswith(".mbox"):
                found.append((segments + [fn[:-5]], full))
    return found


def _display_name(path: str) -> str:
    name = os.path.basename(os.path.normpath(path))
    return name[:-5] if name.lower().endswith(".mbox") else name


# ---------------------------------------------------------------------------
# Adapter classes
# ---------------------------------------------------------------------------

class MboxFile:
    """Stands in for pypff.file."""

    def __init__(self, path: str):
        self.path = path
        self._root = MboxFolder(_display_name(path))

        if os.path.isdir(path):
            boxes = _find_mailboxes(path)
            if not boxes:
                raise ValueError("No mbox files found in directory")
        else:
            boxes = [([], path)]

        for segments, box_path in boxes:
            folder = self._root
            for seg in segments:
                folder = folder.child(seg)
            folder.attach(box_path)

        # A lone mbox file: put its messages in a folder under the root, so the
        # tree mirrors a PST (root → folder with messages).
        if not os.path.isdir(path):
            box = self._root
            self._root = MboxFolder("(root)")
            self._root.sub_folders.append(box)

    def get_root_folder(self):
        return self._root

    def close(self):
        self._root.close()


class MboxFolder:
    """Stands in for pypff.folder."""

    def __init__(self, name: str):
        self.name = name
        self.sub_folders = []
        self._box = None
        self._keys = []

    def child(self, name: str) -> "MboxFolder":
        for f in self.sub_folders:
            if f.name == name:
                return f
        f = MboxFolder(name)
        self.sub_folders.append(f)
        return f

    def attach(self, box_path: str):
        # mailbox.mbox scans the file once for "From " separators to build an
        # index; individual messages are then read on demand by offset.
        self._box = mailbox.mbox(box_path, create=False)
        self._keys = list(self._box.keys())

    @property
    def number_of_sub_folders(self) -> int:
        return len(self.sub_folders)

    def get_sub_folder(self, i: int) -> "MboxFolder":
        return self.sub_folders[i]

    @property
    def number_of_sub_messages(self) -> int:
        return len(self._keys)

    def get_sub_message(self, i: int) -> "MboxMessage":
        return MboxMessage(self._box, self._keys[i])

    def close(self):
        if self._box is not None:
            try:
                self._box.close()
            except Exception:
                pass
        for f in self.sub_folders:
            f.close()


class MboxMessage:
    """
    Stands in for pypff.message. Headers are parsed eagerly (cheap — used by
    listing and search); the full MIME body is parsed only when a body or
    attachment attribute is first accessed.
    """

    def __init__(self, box, key):
        self._box = box
        self._key = key
        self._full = None
        raw = box.get_bytes(key)
        self._raw = raw
        self._headers = BytesHeaderParser(policy=policy.default).parsebytes(raw)

    def _hdr(self, name: str) -> str:
        try:
            v = self._headers.get(name)
            return str(v) if v is not None else ""
        except Exception:
            # Malformed header that the default policy can't decode
            return str(self._headers.get(name, "") or "")

    def _msg(self):
        if self._full is None:
            self._full = email.message_from_bytes(self._raw, policy=policy.default)
        return self._full

    # --- header-derived attributes -----------------------------------------

    @property
    def subject(self) -> str:
        return self._hdr("Subject")

    @property
    def sender_name(self) -> str:
        name, addr = parseaddr(self._hdr("From"))
        return name or addr

    @property
    def delivery_time(self):
        try:
            return parsedate_to_datetime(self._hdr("Date"))
        except Exception:
            return None

    @property
    def transport_headers(self) -> str:
        # One unfolded, decoded line per header — pst_reader parses this block
        # line-by-line, so folded (multi-line) To/Cc must be joined here.
        lines = []
        for name in _HEADER_NAMES:
            value = self._hdr(name)
            if value:
                if name in ("To", "Cc"):
                    value = ", ".join(
                        f"{n} <{a}>" if n and a else (n or a)
                        for n, a in getaddresses([value])
                    )
                lines.append(f"{name}: {' '.join(value.split())}")
        return "\r\n".join(lines)

    # --- body / attachments ------------------------------------------------

    def _body_part(self, subtype: str):
        try:
            part = self._msg().get_body(preferencelist=(subtype,))
            if part is None:
                return None
            return part.get_content().encode("utf-8", errors="replace")
        except Exception:
            return None

    @property
    def html_body(self):
        return self._body_part("html")

    @property
    def plain_text_body(self):
        return self._body_part("plain")

    def _attachments(self):
        try:
            return list(self._msg().iter_attachments())
        except Exception:
            return []

    @property
    def number_of_attachments(self) -> int:
        return len(self._attachments())

    def get_attachment(self, i: int) -> "MboxAttachment":
        return MboxAttachment(self._attachments()[i])


class MboxAttachment:
    """Stands in for pypff.attachment."""

    def __init__(self, part):
        self._part = part

    @property
    def filename(self) -> str:
        return self._part.get_filename() or ""

    @property
    def is_message(self) -> bool:
        return self._part.get_content_type() == "message/rfc822"

    @property
    def data(self) -> bytes:
        """The decoded attachment bytes (an attached email comes back as .eml bytes)."""
        if self.is_message:
            inner = self._part.get_payload()
            if isinstance(inner, list) and inner:
                return inner[0].as_bytes()
            return self._part.as_bytes()
        payload = self._part.get_payload(decode=True)
        return payload or b""

    @property
    def size(self) -> int:
        try:
            return len(self.data)
        except Exception:
            return 0
