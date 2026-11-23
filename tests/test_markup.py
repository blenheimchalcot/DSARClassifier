import json
import threading
import urllib.request
from pathlib import Path

import pytest

from dsar.toolbox.redaction import markup

TRANSCRIPT = """Meeting Metadata
Title: Interview (Alex)
Participants: Alex Example, Sam Staff

Transcript
00:00:01.0 - 00:00:05.0 Sam Staff: Welcome, tell us about yourself.
00:00:06.0 - 00:00:20.0 Alex Example: I live with Pat Neighbour next door.
00:00:21.0 - 00:00:25.0 Alex Example: And I like maths.
00:00:26.0 - 00:00:30.0 Sam Staff: Thanks, bye.
"""


@pytest.fixture
def folder(tmp_path: Path) -> markup.Folder:
    (tmp_path / "01_transcript.txt").write_text(TRANSCRIPT)
    (tmp_path / "02_email.txt").write_text("Subject: Hello\n\nAlex, Pat Neighbour called.\n")
    return markup.Folder(tmp_path, "Reviewer One", tmp_path / "out")


def _doc(folder, name):
    return next(d for d in folder.docs() if d["name"] == name)


def test_elements_are_slices_of_the_file():
    doc = markup.parse(TRANSCRIPT)
    assert doc["kind"] == "transcript"
    assert [e["label"] for e in doc["elements"]] == ["Meeting details", "Sam Staff", "Alex Example", "Sam Staff"]
    alex = doc["elements"][2]
    assert len(alex["parts"]) == 2                                   # consecutive turns merged into one element
    a, b = alex["parts"][0]
    assert TRANSCRIPT[a:b].strip() == "I live with Pat Neighbour next door."
    email = markup.parse("Subject: Hi\nFrom: Sam\n\nFirst para.\n\nSecond para.\n")
    assert [e["label"] for e in email["elements"]] == ["Header", "Paragraph 1", "Paragraph 2"]


def test_release_full_and_partial_decisions_and_export(folder: markup.Folder):
    doc = _doc(folder, "01_transcript.txt")
    base = {"file": doc["name"], "sha256": doc["sha256"], "action": "decide"}
    els = doc["elements"]
    folder.act({**base, "element": els[0]["id"], "decision": "release"})
    folder.act({**base, "element": els[1]["id"], "decision": "redact_full", "reason": markup.REASONS[1]})
    start = TRANSCRIPT.index("Pat Neighbour")
    folder.act({**base, "element": els[2]["id"], "decision": "redact_partial",
                "redactions": [{"start": start, "end": start + 13, "reason": markup.REASONS[0]}]})
    folder.act({**base, "element": els[3]["id"], "decision": "release"})
    doc = _doc(folder, doc["name"])
    assert [doc["decisions"][e["id"]]["decision"] for e in doc["elements"]] == [
        "release", "redact_full", "redact_partial", "release"]
    page, path = folder.export(doc["name"])
    assert "Pat Neighbour" not in page and "tell us about yourself" not in page      # removed, not hidden
    assert "I live with" in page and "I like maths" in page                         # the rest is released
    assert "[Redacted 2: Third-party personal data]" in page and "DRAFT" not in page
    assert path.read_text() == page and folder.events.verify()
    assert '"markup_export"' in (folder.case_output / "AUDIT_LOG.jsonl").read_text()


def test_validation_and_reset(folder: markup.Folder):
    doc = _doc(folder, "02_email.txt")
    el = doc["elements"][-1]
    base = {"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el["id"]}
    a, b = el["parts"][0]
    for bad in ({"decision": "redact_full", "reason": "Because"},
                {"decision": "redact_full", "reason": markup.REASONS[-1]},                 # Other needs a note
                {"decision": "redact_partial", "redactions": []},
                {"decision": "redact_partial", "redactions": [{"start": 0, "end": 3, "reason": markup.REASONS[0]}]},
                {"decision": "release", "sha256": "0" * 64}):                               # stale page
        with pytest.raises(ValueError):
            folder.act({**base, **bad})
    folder.act({**base, "decision": "redact_partial", "redactions": [{"start": a, "end": a + 4, "reason": markup.REASONS[0]}]})
    folder.act({"file": doc["name"], "sha256": doc["sha256"], "action": "reset", "element": el["id"]})
    doc = _doc(folder, "02_email.txt")
    assert doc["decisions"] == {} and doc["redactions"] == []
    assert "DRAFT: 2 of 2" in folder.export(doc["name"])[0]


def test_speaker_name_broken_across_a_line():
    text = TRANSCRIPT.replace("00:00:26.0 - 00:00:30.0 Sam Staff:", "00:00:26.0 - 00:00:30.0 Sam\nStaff:")
    assert [e["who"] for e in markup.parse(text)["elements"][1:]] == ["Sam Staff", "Alex Example", "Sam Staff"]
    # The first parser folded that turn into the previous speaker's element.
    assert len(markup.parse(text, _turn=markup._TURN_V1)["elements"]) == 3


def test_decisions_made_before_a_split_must_be_made_again(folder: markup.Folder):
    text = TRANSCRIPT.replace("00:00:26.0 - 00:00:30.0 Sam Staff:", "00:00:26.0 - 00:00:30.0 Sam\nStaff:")
    (folder.folder / "01_transcript.txt").write_text(text)
    sha = markup.audit.sha256_file(folder.folder / "01_transcript.txt")
    old = markup.parse(text, _turn=markup._TURN_V1, _positional=True)["elements"]
    # Events as the first version wrote them: positional ids, no span.
    for el, decision in zip(old, ["release", "release", "redact_full"]):
        folder.events.append({"file": "01_transcript.txt", "sha256": sha, "reviewer": "R", "action": "decide",
                              "element": el["id"], "decision": decision, "redactions": []})
    doc = _doc(folder, "01_transcript.txt")
    els = doc["elements"]
    assert [e["id"] in doc["decisions"] for e in els] == [True, True, False, False]   # last two were one element
    assert doc["earlier"][els[3]["id"]][0]["decision"] == "redact_full"
    # A reviewer split: the element's decision no longer applies; undoing the split restores it.
    at = text.index("next door")
    folder.act({"file": "01_transcript.txt", "sha256": sha, "action": "split", "at": at, "who": "Pat Neighbour"})
    doc = _doc(folder, "01_transcript.txt")
    new = next(e for e in doc["elements"] if e.get("split"))
    assert new["who"] == "Pat Neighbour" and new["id"] == f"e{at}" and new["id"] in doc["earlier"]
    folder.act({"file": "01_transcript.txt", "sha256": sha, "action": "unsplit", "at": at})
    assert len(_doc(folder, "01_transcript.txt")["elements"]) == 4
    with pytest.raises(ValueError):
        folder.act({"file": "01_transcript.txt", "sha256": sha, "action": "split", "at": at + 1})   # mid-word


def test_model_scores_are_shown_per_element(folder: markup.Folder):
    doc = _doc(folder, "01_transcript.txt")
    el = doc["elements"][2]
    row = {"file": doc["name"], "sha256": doc["sha256"], "span": el["parts"], "score": 7, "description": "Specific PI",
           "evidence": None, "special": None, "model": "m", "prompt_sha256": "p"}
    (folder.folder / markup.SCORES_NAME).write_text(json.dumps(row) + "\n")
    doc = _doc(folder, "01_transcript.txt")
    assert doc["elements"][2]["score"]["score"] == 7 and doc["elements"][1]["score"] is None
    assert doc["decisions"] == {}                                   # a hint, not a decision


def test_extracts_merge_duplicates_and_leave_out_redactions(folder: markup.Folder):
    (folder.folder / "03_copy.txt").write_text("Alex, Pat Neighbour called.\n")
    for d in folder.docs():
        for el in d["elements"]:
            body = {"file": d["name"], "sha256": d["sha256"], "action": "decide", "element": el["id"]}
            if el["who"] == "Sam Staff":
                folder.act({**body, "decision": "redact_full", "reason": markup.REASONS[1]})
            elif "Pat Neighbour" in markup.element_text(d["text"], el) and d["name"] == "01_transcript.txt":
                a = d["text"].index("Pat Neighbour")
                folder.act({**body, "decision": "redact_partial",
                            "redactions": [{"start": a, "end": a + 13, "reason": markup.REASONS[0]}]})
            else:
                folder.act({**body, "decision": "release"})
    result = folder.export_extracts()
    report = Path(result["report"]).read_text()
    assert "Welcome" not in report and "I live with Pat" not in report          # redacted text left out
    assert "I live with" in report and "I like maths" in report
    assert report.count("Alex, Pat Neighbour called.") == 1                      # same text in two files, once
    assert result["merged_duplicates"] == 1 and not result["undecided"]
    register = Path(result["register"]).read_text()
    assert register.count("Alex, Pat Neighbour called.") == 2                    # both sources kept in the register
    assert "markup_extracts" in (folder.case_output / "AUDIT_LOG.jsonl").read_text()


def test_decisions_on_a_changed_file_are_set_aside(folder: markup.Folder):
    doc = _doc(folder, "02_email.txt")
    folder.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide",
                "element": doc["elements"][0]["id"], "decision": "release"})
    (folder.folder / "02_email.txt").write_text("changed")
    doc = _doc(folder, "02_email.txt")
    assert doc["decisions"] == {} and doc["stale"] == 1


def test_server_needs_token_and_stays_in_folder(folder: markup.Folder):
    server, url = markup.serve(folder.folder, "Reviewer One", None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = url.split("/?")[0]
        token = url.split("token=")[1]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/api/docs")
        assert err.value.code == 403
        err.value.close()
        docs = json.load(urllib.request.urlopen(f"{base}/api/docs?token={token}"))["docs"]
        assert [d["name"] for d in docs] == ["01_transcript.txt", "02_email.txt"]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/export?token={token}&file=../secret.txt")
        assert err.value.code == 404
        err.value.close()
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()


def test_an_element_extracted_in_part_is_one_extract_with_its_lines(tmp_path):
    """Cutting names out of a block must not split the rest into separate extracts or run its lines together."""
    folder = tmp_path / "f"
    folder.mkdir()
    text = ("Meeting Metadata\nTitle: Interview (Sam)\nHost: Pat Person\nParticipants: Sam Sample, Pat Person\n"
            "Date: 21 Jul 2026\n")
    (folder / "01_meeting.txt").write_text(text)
    f = markup.Folder(folder, "R", None)
    doc = f.docs()[0]
    el = doc["elements"][0]
    cut = lambda s: {"start": text.index(s), "end": text.index(s) + len(s), "reason": markup.REASONS[0]}
    f.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el["id"],
           "decision": "redact_partial",
           "redactions": [cut("Pat Person\n"), cut(", Pat Person"), cut("Date: 21 Jul 2026")]})
    items = markup.extracts(f.docs())
    assert len(items) == 1
    assert items[0]["text"] == "Meeting Metadata\nTitle: Interview (Sam)\nHost:\nParticipants: Sam Sample"
    # The cut "Pat Person\n" spanned a line break: the break stays, so "Host:" and "Participants" stay on separate lines.


def test_repeats_follow_the_reviewers_decisions_and_apply_with_one_click(tmp_path):
    from dsar import webapp
    from tests.test_webapp import client_for

    data = tmp_path / "DSAR-REP-1" / "data"
    (data / "output").mkdir(parents=True)
    a, b = data / "output" / "deep", data / "output" / "stack"
    a.mkdir(), b.mkdir()
    para = {"ext": "Sam Sample led the toddler group for five years.",
            "out": "Pat Person will send the contract to the team on Friday.",
            "mix": "This sentence was extracted in one place and left out in another."}
    (a / "01_a.txt").write_text(f"{para['ext']}\n\n{para['out']}\n\n{para['mix']}\n\nKind regards\n")
    (a / "02_b.txt").write_text(f"{para['mix']}\n")
    (b / "01_c.txt").write_text(f"{para['ext']}\n\n{para['out']}\n\n{para['mix']}\n\nKind regards\n\nSomething new about Sam.\n")
    fa = markup.Folder(a, "R", None)
    for d in fa.docs():
        for el in d["elements"]:
            txt = markup.element_text(d["text"], el)
            body = {"file": d["name"], "sha256": d["sha256"], "action": "decide", "element": el["id"]}
            if txt.startswith("Sam Sample") or (txt.startswith("This sentence") and d["name"] == "01_a.txt"):
                fa.act({**body, "decision": "release"})
            else:
                fa.act({**body, "decision": "redact_full", "reason": markup.REASONS[0]})
    fb = markup.Folder(b, "R", None)
    sources = markup.repeat_sources([(a, fa.docs()), (b, fb.docs())])
    reps = markup.repeats_for(fb.docs(), sources)["01_c.txt"]
    by_text = {markup.element_text(fb.docs()[0]["text"], el).strip(): reps.get(el["id"]) for el in fb.docs()[0]["elements"]}
    assert by_text[para["ext"]]["reason"] == markup.DUPLICATE             # extracted elsewhere: a duplicate here
    assert by_text[para["out"]]["reason"] == markup.REASONS[0]            # same real reason copied
    assert by_text["Kind regards"]["reason"] == markup.REASONS[0]         # short text: exact match only
    assert by_text[para["mix"]] is None                                   # decided both ways: you decide
    assert by_text["Something new about Sam."] is None                    # new text: you decide

    app, c = client_for(webapp.Settings.for_case(data))
    with app.test_request_context():
        from dsar.toolbox.redaction import web
        fid = next(k for k, p in web.folders().items() if p.name == "stack")
    j = c.get(f"/toolbox/redaction/f/{fid}/api/docs").get_json()
    assert len(j["repeats"]["01_c.txt"]) == 3
    r = c.post(f"/toolbox/redaction/f/{fid}/api/apply-repeats", json={}, headers={"X-CSRF": c.csrf})
    assert r.get_json()["applied"] == 3
    events = [e for e in map(__import__("json").loads, fb.events.lines()) if e.get("copied_from")]
    assert len(events) == 3 and all(e["reviewer"] == "Test Reviewer" and e["copied_from"].startswith("deep/") for e in events)
    left = [el for d in markup.Folder(b, "", None).docs() for el in d["elements"] if el["id"] not in d["decisions"]]
    assert len(left) == 2


def test_emoji_are_one_character_on_screen():
    text = "Hi 🚀 there: Summary"
    shown = markup.screen_text(text)
    assert len(shown) == len(text) and shown == "Hi □ there: Summary"
    assert len(shown.encode("utf-16-le")) // 2 == len(text)              # what the browser counts


def test_only_elements_scored_7_or_more_need_a_decision(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "01_a.txt").write_text("Sam Sample's grievance about the rota.\n\nKind regards, Pat\n\nNot scored yet.\n")
    f = markup.Folder(folder, "R", None)
    doc = f.docs()[0]
    with (folder / markup.SCORES_NAME).open("w") as fh:
        for el, score in zip(doc["elements"], (8, 2)):
            fh.write(json.dumps({"file": doc["name"], "sha256": doc["sha256"], "span": el["parts"], "score": score}) + "\n")
    doc = f.docs()[0]
    assert [el["below"] for el in doc["elements"]] == [False, True, False]
    assert markup.undecided_count(doc) == 2                    # the 8 and the unscored one; the 2 needs nothing
    assert markup.extracts([doc]) == []                         # and nothing below 7 goes in by itself


def test_large_items_send_only_lines_naming_the_subject_to_the_model(tmp_path, monkeypatch):
    import asyncio
    from dsar import classify
    from dsar.chunking.prefilter import build_matcher
    from dsar.config import SubjectProfile

    folder = tmp_path / "f"
    folder.mkdir()
    report = "\n\n".join([f"Learner {i} | On programme" for i in range(60)] + ["Sam Sample | Gateway"]) + "\n"
    (folder / "01_report.txt").write_text(report)
    (folder / "02_note.txt").write_text("A short note.\n\nNothing about anyone.\n")
    sent = []

    class Resp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "7 | Names the subject | Sam Sample"}}

    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json): sent.append(json["messages"][-1]["content"]); return Resp("")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(classify, "parse_response", lambda c: classify.ParsedResponse(7, "Names the subject", "Sam Sample", None, c))
    matcher = build_matcher(SubjectProfile(name="Sam Sample", aliases=["Sam Sample"], emails=[]))
    result = asyncio.run(markup.score_folder(folder, system_prompt="p", model="m", subject_name="Sam Sample",
                                             max_tokens=10, temperature=0, concurrency=1, matcher=matcher,
                                             prefilter_min_elements=50))
    assert result["prefiltered"] == 60                          # the other learners' lines
    assert len(sent) == 3                                       # Sam's line + both lines of the small note
    assert any("Sam Sample | Gateway" in s for s in sent)
    rows = [json.loads(l) for l in (folder / markup.SCORES_NAME).read_text().splitlines()]
    assert sum(r.get("method") == "alias-prefilter" and r["score"] == 0 for r in rows) == 60
    doc = next(d for d in markup.Folder(folder, "R", None).docs() if d["name"] == "01_report.txt")
    assert markup.undecided_count(doc) == 1                     # only Sam's line needs a decision


def test_lines_of_items_at_the_review_line_carry_the_item_score(tmp_path):
    from dsar.chunking.prefilter import build_matcher
    from dsar.config import SubjectProfile

    folder = tmp_path / "f"
    folder.mkdir()
    report = "\n\n".join([f"Learner {i} | On programme" for i in range(60)] + ["Sam Sample | Gateway"]) + "\n"
    (folder / "01_report.txt").write_text(report)
    (folder / "02_row.txt").write_text("ULN | Name | Address\n\n123 | Sam Sample | 1 Example Road\n")
    (folder / "03_low.txt").write_text("Sam Sample was copied in.\n")
    sha = {p.name: markup.audit.sha256_file(p) for p in folder.glob("*.txt")}
    docs = {d["name"]: d for d in markup.Folder(folder, "", None).docs()}
    # The model saw each line on its own and scored Sam's row 1, a lone name 0.
    with (folder / markup.SCORES_NAME).open("w") as fh:
        for name, d in docs.items():
            for el in d["elements"]:
                fh.write(json.dumps({"file": name, "sha256": sha[name], "span": el["parts"], "score": 1}) + "\n")
    matcher = build_matcher(SubjectProfile(name="Sam Sample", aliases=["Sam Sample"], emails=[]))
    n = markup.carry_item_scores(folder, {"01_report.txt": 7, "02_row.txt": markup.REVIEW_MIN_SCORE, "03_low.txt": 1},
                                 matcher=matcher, short_max=50, model="m", prompt_sha="p")
    assert n == 3                       # Sam's report line, and both lines of the short row item; not the item scored 1
    docs = {d["name"]: d for d in markup.Folder(folder, "", None).docs()}
    assert markup.undecided_count(docs["01_report.txt"]) == 1          # the other 60 learners stay below the line
    assert markup.undecided_count(docs["02_row.txt"]) == 2
    assert markup.undecided_count(docs["03_low.txt"]) == 0
    rows = [json.loads(l) for l in (folder / markup.SCORES_NAME).read_text().splitlines()]
    assert all("Model's own line score: 1" in r["description"] for r in rows if r.get("method") == "item-score-carried")
    assert markup.carry_item_scores(folder, {"01_report.txt": 7}, matcher=matcher, short_max=50) == 0   # idempotent


def test_docs_cache_follows_every_change(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "a.txt").write_text("First paragraph.\n\nSecond paragraph.\n")
    f = markup.Folder(folder, "R", None)
    d = f.docs()[0]
    d["elements"][0]["extra"] = 1                                   # a caller's change never reaches the cache
    assert "extra" not in markup.Folder(folder, "R", None).docs()[0]["elements"][0]
    f.act({"file": "a.txt", "sha256": d["sha256"], "action": "decide", "element": d["elements"][0]["id"],
           "decision": "release"})
    assert len(markup.Folder(folder, "R", None).docs()[0]["decisions"]) == 1     # the new event is seen at once
    (folder / "b.txt").write_text("Another file.\n")
    assert len(markup.Folder(folder, "R", None).docs()) == 2
    assert markup.Folder(folder, "R", None).events.verify()


def test_page_carries_the_review_line_and_the_subjects_names():
    page = markup.app_page(["Sam Sample", "sam@example.com", "SS"])
    assert f"const MIN={markup.REVIEW_MIN_SCORE}," in page
    assert '"Sam Sample"' in page and '"sam@example.com"' in page and '"SS"' not in page   # too short to highlight
    assert "__" + "NAMES__" not in page and "__" + "MIN__" not in page
