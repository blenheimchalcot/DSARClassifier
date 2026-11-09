"""
SubstrateFiles/SPOOLS JSONs are Teams chat conversations exported via Purview.
Each file is one conversation thread; the body is in
`content.<guid>:content.value.text` with `\\r\\n` separators between messages
and speaker names interleaved. Sender metadata (emails, timestamps) is in
`properties.SenderEmailAddressList` / `MessageClientSubmitTimeList`.

We emit one chunk per file. The sender email list is woven into the chunk
content so the alias matcher hits the data subject when present even if their
display name doesn't appear in the body verbatim.
"""
import json
from pathlib import Path
from typing import Any, Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


def extract(path: Path, matcher: AliasMatcher, max_chars: int = 8000, overlap_chars: int = 200) -> Iterator[Chunk]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), "raw"),
            source_type="chat_message",
            source_path=str(path),
            item_ref="raw",
            subject=path.stem,
            content=f"[unparseable JSON: {exc}]",
            metadata={"parse_error": str(exc)},
            alias_matched=matcher.matches(path.stem, raw),
        )
        return

    body_text = _extract_body_text(doc)
    props = (doc.get("properties") or {}) if isinstance(doc, dict) else {}

    senders = _list_field(props, "SenderEmailAddressList") or _list_field(props, "SenderSMTPAddressList")
    sender_names = _list_field(props, "SenderNameList") or _list_field(props, "DeduplicatedSenderSimpleDisplayNames")
    times = _list_field(props, "MessageClientSubmitTimeList")
    topic = props.get("Topic") or props.get("ConversationTopic") or ""
    thread_id = props.get("ClientThreadId") or props.get("ConversationThreadId") or ""

    header_lines: list[str] = []
    if topic:
        header_lines.append(f"Topic: {topic}")
    if sender_names:
        header_lines.append("Participants: " + ", ".join(_unique_preserving_order(sender_names)))
    if senders:
        header_lines.append("Sender emails: " + ", ".join(_unique_preserving_order(senders)))
    if times:
        header_lines.append(f"First message: {times[0]} | Last message: {times[-1]}")

    full_content = ("\n".join(header_lines) + "\n\n" + body_text).strip() if body_text or header_lines else ""
    if not full_content:
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), "empty"),
            source_type="chat_message",
            source_path=str(path),
            item_ref="empty",
            subject=topic or path.stem,
            content="[empty chat record]",
            metadata={},
            alias_matched=False,
        )
        return

    parts = split_long(full_content, max_chars=max_chars, overlap_chars=overlap_chars)
    for i, part in enumerate(parts):
        item_ref = "conversation" if len(parts) == 1 else f"conversation#part-{i + 1}"
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), item_ref),
            source_type="chat_message",
            source_path=str(path),
            item_ref=item_ref,
            subject=topic or path.stem,
            content=part,
            metadata={
                "thread_id": thread_id,
                "participants": list(_unique_preserving_order(sender_names or [])),
                "sender_emails": list(_unique_preserving_order(senders or [])),
                "first_sent": times[0] if times else None,
                "last_sent": times[-1] if times else None,
            },
            alias_matched=matcher.matches(part, topic, *(senders or []), *(sender_names or [])),
            thread_id=thread_id or None,
        )


def _extract_body_text(doc: Any) -> str:
    """The body is stored under content.<guid>:content.value.text. The guid
    varies, so we search recursively for any value where we find that shape."""
    if not isinstance(doc, dict):
        return ""
    content = doc.get("content")
    if not isinstance(content, dict):
        return ""
    for v in content.values():
        if isinstance(v, dict):
            inner = v.get("value")
            if isinstance(inner, dict):
                text = inner.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return ""


def _list_field(props: dict, key: str) -> list[str]:
    v = props.get(key)
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    if isinstance(v, str) and v:
        # Some keys are semicolon-separated strings in this export format.
        return [p.strip() for p in v.split(";") if p.strip()]
    return []


def _unique_preserving_order(items):
    seen = set()
    for x in items:
        if x not in seen:
            seen.add(x)
            yield x
