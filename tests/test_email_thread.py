from dsar.chunking.email_thread import (
    hash_paragraph,
    normalise_subject,
    split_paragraphs,
    strip_quoted_history,
    thread_key_from_headers,
)


# --- normalise_subject ---------------------------------------------------------

def test_normalise_subject_strips_re_prefix():
    assert normalise_subject("RE: Project update") == "Project update"


def test_normalise_subject_strips_nested_prefixes():
    assert normalise_subject("Re: Fwd: RE: Budget") == "Budget"


def test_normalise_subject_handles_language_variants():
    assert normalise_subject("AW: Projekt-Update") == "Projekt-Update"
    assert normalise_subject("Fwd: notes") == "notes"


def test_normalise_subject_collapses_whitespace():
    assert normalise_subject("Re:    Project   update  ") == "Project update"


def test_normalise_subject_empty():
    assert normalise_subject("") == ""
    assert normalise_subject(None) == ""


# --- strip_quoted_history ------------------------------------------------------

def test_strip_outlook_original_message_block():
    body = """Thanks for the update.

-----Original Message-----
From: Jane
Sent: 2026-04-20
Subject: Hi
Older content that should be stripped."""
    out = strip_quoted_history(body)
    assert "Thanks for the update" in out
    assert "Older content" not in out


def test_strip_gmail_on_wrote_block():
    body = """Here's my reply.

On Mon, 20 Apr 2026 at 10:00, Jane Doe <jane@example.com> wrote:
> hi team
> earlier message content"""
    out = strip_quoted_history(body)
    assert "Here's my reply" in out
    assert "earlier message content" not in out


def test_strip_outlook_horizontal_rule():
    body = "My reply.\n\n________________________________\nFrom: Jane\nSubject: Old"
    out = strip_quoted_history(body)
    assert out.strip() == "My reply."


def test_strip_plain_text_quote_block():
    body = "Reply text.\n\n> old line one\n> old line two\n> old line three\n> old line four"
    out = strip_quoted_history(body)
    assert "Reply text." in out
    assert "old line" not in out


def test_strip_outlook_header_block():
    body = """Short reply.

From: Jane Doe
Sent: Mon 20 Apr 2026
To: Team
Subject: Previous
Content of the original message."""
    out = strip_quoted_history(body)
    assert "Short reply" in out
    assert "Content of the original message" not in out


def test_strip_leaves_body_when_no_quote_markers():
    body = "Just a standalone note."
    assert strip_quoted_history(body) == body


# --- paragraph hashing ---------------------------------------------------------

def test_hash_paragraph_is_deterministic():
    assert hash_paragraph("The Emma Banks HR review is scheduled next week.") == \
           hash_paragraph("the emma banks hr review is scheduled NEXT week.")


def test_hash_paragraph_ignores_punctuation_and_whitespace():
    a = hash_paragraph("Please review the report for compliance before Friday.")
    b = hash_paragraph("Please   review the report, for compliance! Before Friday.")
    assert a == b


def test_hash_paragraph_short_text_returns_empty():
    # Below MIN_PARAGRAPH_CHARS — returning "" means we won't dedupe on trivially short text.
    assert hash_paragraph("Thanks") == ""
    assert hash_paragraph("ok") == ""


def test_hash_paragraph_different_content_different_hash():
    a = hash_paragraph("We are reviewing the compliance report this Friday at ten.")
    b = hash_paragraph("The budget forecast for next quarter was approved today.")
    assert a != b and a and b


# --- split_paragraphs ----------------------------------------------------------

def test_split_paragraphs_splits_on_blank_lines():
    text = "first\n\nsecond paragraph\n\nthird"
    assert split_paragraphs(text) == ["first", "second paragraph", "third"]


def test_split_paragraphs_ignores_trailing_whitespace():
    text = "only one  \n\n  "
    assert split_paragraphs(text) == ["only one"]


# --- thread_key_from_headers ---------------------------------------------------

def test_thread_key_prefers_references_root():
    headers = "References: <root@ex.com> <reply@ex.com>\nIn-Reply-To: <reply@ex.com>\n"
    a = thread_key_from_headers(headers, "RE: Hi", "123")
    b = thread_key_from_headers(headers, "RE: Hi", "456")
    assert a == b  # same thread, different identifiers


def test_thread_key_falls_back_to_in_reply_to():
    headers = "In-Reply-To: <parent@ex.com>\n"
    a = thread_key_from_headers(headers, "Re: topic", "1")
    b = thread_key_from_headers(headers, "Fwd: topic", "2")
    assert a == b


def test_thread_key_falls_back_to_normalised_subject():
    a = thread_key_from_headers("", "RE: Project X", "1")
    b = thread_key_from_headers("", "FW: Project X", "2")
    assert a == b
    assert a.startswith("subj-")


def test_thread_key_singleton_when_no_subject_or_headers():
    a = thread_key_from_headers("", "", "msg-1")
    b = thread_key_from_headers("", "", "msg-2")
    assert a != b
    assert a.startswith("msg-")
