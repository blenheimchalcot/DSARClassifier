from pathlib import Path

from dsar.chunking.common import hash_chunk_id, split_long, strip_signature
from dsar.chunking.prefilter import build_matcher
from dsar.chunking import documents
from dsar.config import SubjectProfile


def _matcher():
    return build_matcher(SubjectProfile(
        name="Emma Banks",
        aliases=["Emma Banks", "EB", "Emma"],
        emails=["emmabanks12@gmail.com"],
    ))


def test_hash_chunk_id_is_deterministic_and_unique():
    a = hash_chunk_id("/foo.pst", "msg-1")
    b = hash_chunk_id("/foo.pst", "msg-1")
    c = hash_chunk_id("/foo.pst", "msg-2")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_split_long_passes_through_short_text():
    assert split_long("short", max_chars=100, overlap_chars=10) == ["short"]


def test_split_long_splits_on_paragraphs():
    para = "a" * 500
    text = "\n\n".join([para, para, para])
    parts = split_long(text, max_chars=600, overlap_chars=20)
    assert len(parts) >= 2
    for p in parts:
        assert len(p) <= 600 + 20  # overlap may nudge slightly


def test_strip_signature_removes_regards_block():
    body = "Hi team,\nThe update is ready.\n\nRegards,\nJane\nCompany Ltd"
    result = strip_signature(body)
    assert "Regards" not in result
    assert "The update is ready" in result


def test_strip_signature_preserved_if_contains_alias():
    import re
    body = "Hi team,\nAll good.\n\n--\nEmma Banks\nHR"
    preserve = re.compile(r"\bEmma Banks\b")
    result = strip_signature(body, preserve_if_matches=preserve)
    assert "Emma Banks" in result


def test_document_extractor_on_text_file(tmp_path: Path):
    f = tmp_path / "note.txt"
    f.write_text("This is a note about EB in the HR team.")
    chunks = list(documents.extract(f, _matcher(), max_chars=8000, overlap_chars=200))
    assert len(chunks) == 1
    c = chunks[0]
    assert c.source_type == "document"
    assert c.alias_matched is True
    assert "EB" in c.content


def test_document_extractor_no_alias(tmp_path: Path):
    f = tmp_path / "unrelated.txt"
    f.write_text("Quarterly budget review notes.")
    chunks = list(documents.extract(f, _matcher(), max_chars=8000, overlap_chars=200))
    assert len(chunks) == 1
    assert chunks[0].alias_matched is False
