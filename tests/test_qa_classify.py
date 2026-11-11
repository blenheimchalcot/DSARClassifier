import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from openpyxl import load_workbook

from dsar import db, qa_classify
from dsar.chunking import Chunk
from dsar.cli import main
from dsar.config import load_config

CONFIG = """
subject:
  name: "Alex Example"
  aliases: ["Alex Example"]
  emails: []
model:
  default: "test-model"
  max_tokens: 50
  temperature: 0.0
  concurrency: 2
chunking:
  max_chars: 8000
  overlap_chars: 200
qa:
  sample_per_band: 2
"""

# chunk id, content, score, evidence, special return, raw response
ROWS = [
    ("c-high-ok", "Alex Example is a liar and useless.", 9, "is a liar and useless", None, "9 | Derogatory | is a liar and useless"),
    ("c-high-bad", "Alex Example missed the deadline.", 8, "The data subject is an idiot", None, "8 | x | The data subject is an idiot"),
    ("c-missed", "Alex Example was signed off with depression last week.", 2, None, None, "2 | Mentioned |"),
    ("c-low", "Alex Example is on the distribution list.", 1, None, None, "1 | Incidental |"),
    ("c-empty", "", None, None, "[Email empty]", "[Email empty]"),
    ("c-garbled", "Alex Example attended.", None, None, None, "I am not sure what to say here"),
]


@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return load_config(p)


def _build(db_path: Path, rows=ROWS, pending: int = 1) -> None:
    with db.connect(db_path) as conn:
        db.init_schema(conn)
        for cid, content, score, ev, special, raw in rows:
            db.upsert_chunk(conn, Chunk(chunk_id=cid, source_type="email", source_path="/in/x.pst", item_ref=cid,
                                        subject="s", content=content, metadata={}, alias_matched=True))
            db.insert_classification(conn, chunk_id=cid, score=score, description="d", evidence=ev,
                                     special_return=special, raw_response=raw, model="m", input_tokens=1,
                                     output_tokens=1, cache_read_tokens=0)
        for i in range(pending):
            db.upsert_chunk(conn, Chunk(chunk_id=f"pending{i}", source_type="email", source_path="/in/x.pst",
                                        item_ref=f"p{i}", subject="s", content="Alex Example", metadata={},
                                        alias_matched=True))


def test_analyse(cfg, tmp_path: Path):
    _build(tmp_path / "dsar.sqlite")
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        result = qa_classify.analyse(conn, cfg)
    status = {c["name"]: c["status"] for c in result["checks"]}
    assert status == {"Format": qa_classify.FAIL, "Coverage": qa_classify.WARN,
                      "Evidence": qa_classify.WARN, "Special categories": qa_classify.WARN}
    by = {(f["check"], f["chunk_id"]) for f in result["findings"]}
    assert ("evidence", "c-high-bad") in by and ("evidence", "c-high-ok") not in by
    assert ("special", "c-missed") in by and ("special", "c-low") not in by
    assert ("format", "c-garbled") in by
    assert result["stats"]["pending_in_scope"] == 1
    assert result["stats"]["bands"] == {"7-10": 2, "0-4": 2, "special": 2}


def _fake(answer_for: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["messages"][-1]["content"]
        answer = next((a for key, a in answer_for.items() if key in text), "1 | x |")
        return httpx.Response(200, json={"message": {"content": answer}})
    return lambda: httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))


def test_consistency(cfg, tmp_path: Path):
    _build(tmp_path / "dsar.sqlite")
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        rows = qa_classify.analyse(conn, cfg)["rows"]
    same = _fake({"liar": "9 | x | is a liar and useless", "deadline": "8 | x |", "depression": "2 | x |",
                  "distribution": "1 | x |"})
    check, results = qa_classify.consistency(same, cfg, "p", rows, n=10)
    assert check["status"] == qa_classify.PASS and len(results) == 4      # only scored rows are re-scored
    flaky = _fake({"liar": "2 | x |", "deadline": "1 | x |", "depression": "9 | x |", "distribution": "7 | x |"})
    check, _ = qa_classify.consistency(flaky, cfg, "p", rows, n=10)
    assert check["status"] == qa_classify.FAIL and "crossed" in check["detail"]


def test_sample_roundtrip(cfg, tmp_path: Path):
    _build(tmp_path / "dsar.sqlite")
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        rows = qa_classify.analyse(conn, cfg)["rows"]
    sample = qa_classify.stratified_sample(rows, per_band=2)
    assert {r["band"] for r in sample} == {"0-4", "7-10", "special"}
    path = tmp_path / "sample.xlsx"
    qa_classify.write_sample(sample, path)

    wb = load_workbook(path)
    ws = wb["Sample"]
    header = [c.value for c in ws[1]]
    col = {name: header.index(name) + 1 for name in header}
    for r in range(2, ws.max_row + 1):
        model = ws.cell(r, col["model_score"]).value
        chunk = ws.cell(r, col["chunk_id"]).value
        if chunk == "c-garbled":
            continue                                              # left for later: outstanding
        reviewer = -1 if not isinstance(model, int) else (7 if chunk == "c-missed" else model)
        ws.cell(r, col["reviewer_score"]).value = reviewer
        ws.cell(r, col["reviewer_initials"]).value = "QA"
    wb.save(path)

    result = qa_classify.import_sample(path)
    assert result["reviewed"] == 5 and result["outstanding"] == 1 and result["reviewers"] == ["QA"]
    assert [d["chunk_id"] for d in result["disagreements"]] == ["c-missed"]
    assert result["agreement_within_1"] == 0.8


def test_cli(tmp_path: Path):
    data = tmp_path / "data"
    (data / "input").mkdir(parents=True)
    _build(data / "dsar.sqlite", rows=[r for r in ROWS if r[0] != "c-garbled"], pending=0)
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    env = {"DSAR_DATA_DIR": str(data)}
    result = CliRunner().invoke(main, ["qa", "classify", "--config", str(config), "--rescore", "0"], env=env)
    assert result.exit_code == 0, result.output
    out = data / "output"
    assert len(list(out.glob("qa_classify_*.csv"))) == 1
    sample = next(out.glob("qa_sample_*.xlsx"))
    result = CliRunner().invoke(main, ["qa", "import-sample", str(sample)], env=env)
    assert result.exit_code == 0, result.output
    assert "outstanding" in result.output
