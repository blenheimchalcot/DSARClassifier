from dsar.classify import parse_response


def test_parses_low_score_no_evidence():
    p = parse_response("0 | Business acronym")
    assert p.score == 0
    assert p.description == "Business acronym"
    assert p.evidence is None
    assert p.special_return is None


def test_parses_high_score_with_evidence():
    raw = "9 | Behavioral grievance | EB submitted a DSAR... very annoying"
    p = parse_response(raw)
    assert p.score == 9
    assert p.description == "Behavioral grievance"
    assert p.evidence == "EB submitted a DSAR... very annoying"


def test_strips_evidence_when_score_below_seven():
    # Model included evidence even though score < 7 — spec says drop it.
    p = parse_response("5 | Contextual HR mention | extra evidence field")
    assert p.score == 5
    assert p.evidence is None


def test_parses_special_return_email_empty():
    p = parse_response("[Email empty]")
    assert p.score is None
    assert p.special_return == "[Email empty]"


def test_parses_not_able_to_classify():
    p = parse_response("[not able to classify]")
    assert p.score is None
    assert p.special_return == "[not able to classify]"


def test_ignores_preamble_and_picks_first_line():
    raw = "Here is the result:\n\n10 | Derogatory | The data subject is an idiot"
    # First non-empty line is "Here is the result:" — not matching.
    # This test asserts we correctly handle that degeneracy: no integer parse, so score=None.
    # (In practice temperature=0 + the system prompt should keep this from happening.)
    p = parse_response(raw)
    assert p.score is None
