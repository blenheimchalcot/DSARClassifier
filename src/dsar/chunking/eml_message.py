"""
RFC 822 ``.eml`` chunker.

Saved emails turn up both as SharePoint files and as PST attachments. Same
content shape as the ``.msg`` chunker (header block + body, plain → html), so
the classifier sees a familiar message. Attachments inside the .eml are listed
by name in the chunk text so they are visible to the reviewer; their content is
not extracted here.
"""
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        from bs4 import BeautifulSoup  # lazy import
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    return text.strip()


def extract(path: Path, matcher: AliasMatcher, max_chars: int, overlap_chars: int) -> Iterator[Chunk]:
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    fields = {k: str(msg.get(k, "") or "") for k in ("From", "To", "Cc", "Date", "Subject", "Message-ID")}
    attachment_names = [a.get_filename() or "(unnamed)" for a in msg.iter_attachments()]
    header = "\n".join(f"{k}: {fields[k]}" for k in ("From", "To", "Cc", "Date", "Subject"))
    if attachment_names:
        header += "\nAttachments: " + "; ".join(attachment_names)
    full_text = f"{header}\n\n{_body(msg)}".strip()

    parts = split_long(full_text, max_chars=max_chars, overlap_chars=overlap_chars)
    for i, part in enumerate(parts):
        item_ref = "full" if len(parts) == 1 else f"part-{i+1}"
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), item_ref),
            source_type="email",
            source_path=str(path),
            item_ref=item_ref,
            subject=fields["Subject"] or path.stem,
            content=part,
            metadata={
                "format": "eml",
                "sender": fields["From"],
                "to": fields["To"],
                "cc": fields["Cc"],
                "date": fields["Date"],
                "message_id": fields["Message-ID"] or None,
                "attachments": attachment_names or None,
                "size_bytes": path.stat().st_size,
            },
            alias_matched=matcher.matches(part, fields["From"], fields["To"], fields["Cc"], fields["Subject"]),
        )
