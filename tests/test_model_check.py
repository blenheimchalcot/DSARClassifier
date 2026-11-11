import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from dsar import db, model_check
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
"""

# What a well-behaved model answers for each synthetic example.
ANSWERS = [
    ("useless and a liar", "10 | Derogatory personal attack | is useless and a liar"),
    ("grievance", "9 | Behavioural opinion | keeps ignoring the agreed process"),
    ("depression", "8 | Health data | signed off work for six weeks with depression"),
    ("home address", "7 | Home address and DOB | 14 Example Road"),
    ("interview panel", "5 | Mentioned in HR context |"),
    ("bank holiday", "2 | On a distribution list |"),
]
INFO = {"url": "http://ollama", "version": "0.40.13", "model": "test-model", "digest": "c6eb396dbd59ffff"}


@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return load_config(p)


def fake_ollama(truncate_to: int | None = None, always: str | None = None):
    """httpx client factory for a fake Ollama; `truncate_to` drops the end of long prompts."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        if truncate_to is not None:
            text = text[:truncate_to]
        answer = always or next((a for key, a in ANSWERS if key in text), "1 | Nothing about the subject |")
        return httpx.Response(200, json={"message": {"content": answer}, "prompt_eval_count": 100, "eval_count": 10})

    return lambda: httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))


def _status(result: dict) -> dict:
    return {c["name"]: c["status"] for c in result["checks"]}


def test_good_model_passes(cfg):
    result = model_check.run(fake_ollama(), cfg, "system prompt", INFO, lambda: {"context_length": 8192})
    assert result["passed"], result["checks"]
    assert set(_status(result).values()) == {model_check.PASS}
    assert all(g["pass"] for g in result["golden"])
    assert result["throughput"]["sample_source"] == "golden"


def test_truncation_is_caught(cfg):
    # Sees only the first 2,000 chars: the sentence placed last in a full chunk is lost.
    result = model_check.run(fake_ollama(truncate_to=2000), cfg, "system prompt", INFO, lambda: None)
    assert _status(result)["Context window"] == model_check.FAIL
    assert not result["passed"]


def test_inaccurate_model_fails(cfg):
    result = model_check.run(fake_ollama(always="5 | Something |"), cfg, "system prompt", INFO, lambda: None)
    assert _status(result)["Accuracy"] == model_check.FAIL


def test_invented_evidence_fails_the_example(cfg):
    result = model_check.run(fake_ollama(always="9 | Behavioural | The data subject is an idiot"),
                             cfg, "system prompt", INFO, lambda: None)
    assert not any(g["pass"] for g in result["golden"])


def test_unparseable_responses_fail_format(cfg):
    result = model_check.run(fake_ollama(always="I think this is about a person."), cfg, "p", INFO, lambda: None)
    assert _status(result)["Format"] == model_check.FAIL


def test_wrong_digest_fails(cfg):
    cfg.model.expected_digest = "aaaaaaaaaaaa"
    result = model_check.run(fake_ollama(), cfg, "p", INFO, lambda: None)
    assert _status(result)["Model digest"] == model_check.FAIL


def test_case_chunks_timed_and_run_estimated(cfg, tmp_path: Path):
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        db.init_schema(conn)
        for i in range(10):
            db.upsert_chunk(conn, Chunk(chunk_id=f"c{i:02d}", source_type="email", source_path="/x.pst",
                                        item_ref=str(i), subject="s", content=f"Alex Example note {i}",
                                        metadata={}, alias_matched=True))
        conn.commit()
        result = model_check.run(fake_ollama(), cfg, "p", INFO, lambda: None, conn)
    t = result["throughput"]
    assert t["sample_source"] == "case chunks" and t["pending_chunks"] == 10 and t["sample_size"] == 6
    # Case chunk responses are not written to the result document.
    assert "Alex Example note" not in json.dumps(result)


def test_latest_pass_matches_fingerprint(cfg, tmp_path: Path):
    fp = model_check.fingerprint(cfg, "prompt", "digest1")
    (tmp_path / "model_check_20261101T000000Z.json").write_text(json.dumps({"passed": True, "fingerprint": fp}))
    assert model_check.latest_pass(tmp_path, fp).name == "model_check_20261101T000000Z.json"
    # A different prompt, digest or setting is not vouched for.
    assert model_check.latest_pass(tmp_path, model_check.fingerprint(cfg, "edited prompt", "digest1")) is None
    assert model_check.latest_pass(tmp_path, model_check.fingerprint(cfg, "prompt", "digest2")) is None
    # A newer failed check for the same fingerprint wins.
    (tmp_path / "model_check_20261102T000000Z.json").write_text(json.dumps({"passed": False, "fingerprint": fp}))
    assert model_check.latest_pass(tmp_path, fp) is None


def test_context_text_size():
    for first in (True, False):
        text = model_check.context_text("Alex Example", 8000, marker_first=first)
        assert 7000 < len(text) <= 8000
        assert ("depression" in text[:400]) == first


def test_classify_refuses_without_model_check(tmp_path: Path):
    data = tmp_path / "data"
    (data / "input").mkdir(parents=True)
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system prompt", encoding="utf-8")
    env = {"DSAR_DATA_DIR": str(data), "OLLAMA_URL": "http://127.0.0.1:9"}
    args = ["classify", "--config", str(config), "--system-prompt", str(prompt)]

    result = CliRunner().invoke(main, args, env=env)
    assert result.exit_code != 0 and "model-check" in result.output

    result = CliRunner().invoke(main, args + ["--skip-checks", "test run on synthetic data"], env=env)
    assert result.exit_code == 0, result.output
    from dsar import audit
    end = audit.AuditLog(data / "output").entries()[-1]["data"]
    assert end["checks_skipped"] == "test run on synthetic data"


@pytest.mark.live
def test_live_model_check(tmp_path: Path):
    """Real Ollama: `pytest -m live` with the case model pulled and the server running."""
    from dsar.classify import ollama_info, ollama_url
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG.replace("test-model", "gemma4:e4b"), encoding="utf-8")
    cfg = load_config(cfg_path)
    info = ollama_info(cfg.model.default)
    if not info.get("digest"):
        pytest.skip(f"no live model: {info}")
    prompt = (Path(__file__).resolve().parents[1] / "claude_strict.example.md").read_text(encoding="utf-8")
    prompt = prompt.replace("<SUBJECT FULL NAME>", cfg.subject.name)
    result = model_check.run(lambda: httpx.AsyncClient(base_url=ollama_url(), timeout=300.0),
                             cfg, prompt, info, lambda: None)
    print(json.dumps(result["checks"], indent=2))
    assert result["passed"]
