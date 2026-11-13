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
