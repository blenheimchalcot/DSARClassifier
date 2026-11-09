from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    chunk_id: str
    source_type: str  # 'email' | 'document' | 'spreadsheet_row'
    source_path: str
    item_ref: str
    content: str
    subject: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    alias_matched: bool = False
    thread_id: str | None = None
    skipped_as_duplicate_of: str | None = None
