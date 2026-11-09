"""End-to-end dedupe test driving ingest_emails with in-memory fake messages."""
from pathlib import Path

from dsar import db
from dsar.chunking.prefilter import build_matcher
from dsar.chunking.pst import RawMessage, ingest_emails
from dsar.config import SubjectProfile


def _matcher():
    return build_matcher(SubjectProfile(
        name="Emma Banks",
        aliases=["Emma Banks", "EB", "Emma"],
        emails=["emmabanks12@gmail.com"],
    ))


def _msg(ident: str, sent_iso: str, subject: str, body: str, headers: str = "") -> RawMessage:
    return RawMessage(
        pst_path="/tmp/case.pst",
        identifier=ident,
        subject=subject,
        body=body,
        headers_raw=headers,
        sender_name="Alice",
        sent_iso=sent_iso,
        to="team@example.com",
        cc=None,
        sender_header=None,
    )


def test_reply_quoting_original_is_paragraph_deduped(tmp_path: Path):
    """Msg A is original. Msg B quotes A verbatim. With quote-history stripping
    disabled (preserved for DSAR completeness), B is no longer marked as a whole-
    chunk duplicate, but its repeated paragraph is dropped via paragraph dedupe
    and the chunk isn't alias-matched, so it still won't be sent to the classifier."""
    original_para = (
        "We are scheduling an HR review for Emma Banks next Thursday about "
        "her recent performance concerns and the grievance she submitted."
    )
    msg_a = _msg("1", "2026-04-20T09:00:00", "HR review", original_para)
    msg_b = _msg(
        "2", "2026-04-20T10:00:00", "RE: HR review",
        f"-----Original Message-----\nFrom: Alice\n\n{original_para}",
        headers="In-Reply-To: <1@ex.com>\nReferences: <1@ex.com>\n",
    )

    with db.connect(tmp_path / "d.sqlite") as conn:
        db.init_schema(conn)
        chunks = list(ingest_emails(iter([msg_a, msg_b]), _matcher(), conn,
                                    max_chars=8000, overlap_chars=200))
        for c in chunks:
            db.upsert_chunk(conn, c)

    assert len(chunks) == 2
    first, second = chunks
    # Msg A survives with real content and is alias-matched.
    assert first.skipped_as_duplicate_of is None
    assert first.alias_matched is True
    assert original_para in first.content
    # Msg B keeps only the short header preamble (original_para got paragraph-deduped).
    # Alice + the header don't reference Emma, so the chunk is not alias-matched and
    # will be excluded from classification — the desired outcome.
    assert original_para not in second.content
    assert second.alias_matched is False


def test_reply_with_new_content_keeps_only_new_paragraph(tmp_path: Path):
    original_para = (
        "We are scheduling an HR review for Emma Banks next Thursday at 2pm "
        "to discuss her recent performance concerns."
    )
    new_para = (
        "Adding Bob from legal — he should be in the room given the grievance EB filed."
    )
    msg_a = _msg("1", "2026-04-20T09:00:00", "HR review", original_para)
    msg_b_body = (
        f"{new_para}\n\n"
        f"-----Original Message-----\n"
        f"From: Alice\nSubject: HR review\n\n"
        f"{original_para}"
    )
    msg_b = _msg(
        "2", "2026-04-20T10:00:00", "RE: HR review", msg_b_body,
        headers="In-Reply-To: <1@ex.com>\n",
    )

    with db.connect(tmp_path / "d.sqlite") as conn:
        db.init_schema(conn)
        chunks = list(ingest_emails(iter([msg_a, msg_b]), _matcher(), conn,
                                    max_chars=8000, overlap_chars=200))
        for c in chunks:
            db.upsert_chunk(conn, c)

    assert len(chunks) == 2
    first, second = chunks
    assert first.skipped_as_duplicate_of is None
    assert second.skipped_as_duplicate_of is None
    # The reply's content must include the new commentary, not the quoted original.
    assert "Adding Bob from legal" in second.content
    assert original_para not in second.content


def test_same_thread_id_across_reply_chain(tmp_path: Path):
    msg_a = _msg(
        "1", "2026-04-20T09:00:00", "Quarterly review",
        "Scheduling Emma Banks's quarterly review for next Thursday afternoon.",
        headers="Message-ID: <1@ex.com>\n",
    )
    msg_b = _msg(
        "2", "2026-04-20T10:00:00", "RE: Quarterly review",
        "Can we move it to Friday morning instead? That works better for the interviewers.",
        headers="Message-ID: <2@ex.com>\nIn-Reply-To: <1@ex.com>\nReferences: <1@ex.com>\n",
    )

    with db.connect(tmp_path / "d.sqlite") as conn:
        db.init_schema(conn)
        chunks = list(ingest_emails(iter([msg_a, msg_b]), _matcher(), conn,
                                    max_chars=8000, overlap_chars=200))

    assert chunks[0].thread_id == chunks[1].thread_id


def test_unrelated_messages_get_different_threads(tmp_path: Path):
    msg_a = _msg("1", "2026-04-20T09:00:00", "Quarterly review",
                 "Emma Banks's quarterly review next week to discuss her performance goals.")
    msg_b = _msg("2", "2026-04-20T10:00:00", "Lunch plans",
                 "Anyone want to grab lunch at the new place on the corner tomorrow?")

    with db.connect(tmp_path / "d.sqlite") as conn:
        db.init_schema(conn)
        chunks = list(ingest_emails(iter([msg_a, msg_b]), _matcher(), conn,
                                    max_chars=8000, overlap_chars=200))

    assert chunks[0].thread_id != chunks[1].thread_id


def test_chronological_processing_assigns_origin_to_earliest(tmp_path: Path):
    """Messages fed in reverse order should still process oldest-first so the
    origin of a duplicated paragraph is the earliest sender."""
    shared_para = (
        "The DSAR response deadline is thirty days from receipt of the request "
        "and involves review of all mailbox content for the data subject."
    )
    late = _msg("later", "2026-04-22T12:00:00", "DSAR reminder",
                shared_para)
    early = _msg("earlier", "2026-04-20T09:00:00", "DSAR briefing",
                 shared_para)

    with db.connect(tmp_path / "d.sqlite") as conn:
        db.init_schema(conn)
        # Feed in reverse: late first, then early.
        chunks = list(ingest_emails(iter([late, early]), _matcher(), conn,
                                    max_chars=8000, overlap_chars=200))

    # Output order follows sort order = chronological, so early comes first.
    assert chunks[0].item_ref == "msg-earlier"
    assert chunks[0].skipped_as_duplicate_of is None
    assert chunks[1].item_ref == "msg-later"
    assert chunks[1].skipped_as_duplicate_of is not None
