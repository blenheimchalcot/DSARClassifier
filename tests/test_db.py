from pathlib import Path

from dsar import db
from dsar.chunking import Chunk


def _sample_chunk(alias_matched: bool = True) -> Chunk:
    return Chunk(
        chunk_id="deadbeefcafe0001",
        source_type="email",
        source_path="/tmp/case.pst",
        item_ref="msg-42",
        subject="Re: project",
        content="Emma Banks will attend.",
        metadata={"sender": "alice@example.com"},
        alias_matched=alias_matched,
    )


def test_schema_and_upsert_and_unclassified_list(tmp_path: Path):
    db_path = tmp_path / "test.sqlite"
    with db.connect(db_path) as conn:
        db.init_schema(conn)
        db.upsert_chunk(conn, _sample_chunk())

        # Idempotent upsert — repeated call should not error or duplicate.
        db.upsert_chunk(conn, _sample_chunk())

        ids = db.unclassified_chunk_ids(conn)
        assert ids == ["deadbeefcafe0001"]

        db.insert_classification(
            conn,
            chunk_id="deadbeefcafe0001",
            score=7,
            description="Specific PI",
            evidence="home address",
            special_return=None,
            raw_response="7 | Specific PI | home address",
            model="claude-sonnet-4-6",
            input_tokens=1200,
            output_tokens=30,
            cache_read_tokens=1100,
        )

        # Now that it's classified, it should no longer appear in the pending list.
        assert db.unclassified_chunk_ids(conn) == []


def test_non_matched_chunks_are_not_classified(tmp_path: Path):
    db_path = tmp_path / "test.sqlite"
    with db.connect(db_path) as conn:
        db.init_schema(conn)
        db.upsert_chunk(conn, _sample_chunk(alias_matched=False))
        assert db.unclassified_chunk_ids(conn) == []
