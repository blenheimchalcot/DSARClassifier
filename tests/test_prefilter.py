from dsar.chunking.prefilter import build_matcher
from dsar.config import SubjectProfile


def _matcher():
    return build_matcher(SubjectProfile(
        name="Emma Banks",
        aliases=["Emma Banks", "E Banks", "E. Banks", "EB", "Emma"],
        emails=["emmabanks12@gmail.com"],
    ))


def test_matches_full_name():
    m = _matcher()
    assert m.matches("Emma Banks was in the meeting")


def test_matches_initials():
    m = _matcher()
    assert m.matches("I spoke to EB in HR today")


def test_matches_email():
    m = _matcher()
    assert m.matches("Please contact emmabanks12@gmail.com")


def test_matches_case_insensitive():
    m = _matcher()
    assert m.matches("emma banks submitted a form")
    assert m.matches("Talked to eb this morning")


def test_eb_word_boundary_avoids_substrings():
    """EB should NOT match inside 'webinar' or 'ebay'."""
    m = _matcher()
    # "webinar" contains 'eb' — word boundaries must prevent a match.
    # However, 'Emma' still matches on its own; so pick strings with no alias at all.
    assert not m.matches("Join the webinar tomorrow")
    assert not m.matches("Use ebay for procurement")


def test_no_match_unrelated_text():
    m = _matcher()
    assert not m.matches("The quarterly budget was approved.")


def test_matches_across_multiple_fields():
    m = _matcher()
    assert m.matches(None, "no match here", "Cc: EB@company.com")


def test_empty_inputs_dont_match():
    m = _matcher()
    assert not m.matches(None, "", None)
