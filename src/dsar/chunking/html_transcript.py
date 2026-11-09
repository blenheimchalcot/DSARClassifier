"""
MicrosoftMeetingTranscripts HTMLs are Teams meeting records exported via Purview.
Each file starts with an HTML property table (ConversationId, MeetingTitle,
OrganizerId, attendees, etc.) and may include the transcript body underneath.
"""
from pathlib import Path
from typing import Iterator

from dsar.chunking import Chunk
from dsar.chunking.common import hash_chunk_id, split_long
from dsar.chunking.prefilter import AliasMatcher


def extract(path: Path, matcher: AliasMatcher, max_chars: int = 8000, overlap_chars: int = 200) -> Iterator[Chunk]:
    from bs4 import BeautifulSoup  # lazy import

    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception as exc:
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), "raw"),
            source_type="meeting_transcript",
            source_path=str(path),
            item_ref="raw",
            subject=path.stem,
            content=f"[unparseable HTML: {exc}]",
            metadata={"parse_error": str(exc)},
            alias_matched=matcher.matches(path.stem, raw),
        )
        return

    properties: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True)
            val = cells[1].get_text(" ", strip=True)
            if key and key not in properties and not key.endswith("@type"):
                properties[key] = val

    full_text = soup.get_text("\n", strip=True)
    title = properties.get("MeetingTitle") or path.stem

    content = full_text or "[empty transcript]"
    parts = split_long(content, max_chars=max_chars, overlap_chars=overlap_chars)
    for i, part in enumerate(parts):
        item_ref = "transcript" if len(parts) == 1 else f"transcript#part-{i + 1}"
        yield Chunk(
            chunk_id=hash_chunk_id(str(path), item_ref),
            source_type="meeting_transcript",
            source_path=str(path),
            item_ref=item_ref,
            subject=title,
            content=part,
            metadata={
                "ConversationId": properties.get("ConversationId"),
                "ClientThreadId": properties.get("ClientThreadId"),
                "OrganizerId": properties.get("OrganizerId"),
                "InternetMessageId": properties.get("InternetMessageId"),
            },
            alias_matched=matcher.matches(part, title),
        )
