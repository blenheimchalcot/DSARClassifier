import sys
import types
from pathlib import Path

import pytest

from dsar import doctor
from dsar.config import load_config

CONFIG = """
subject:
  name: "Alex Example"
  aliases: ["Alex Example"]
  emails: []
model:
  default: "gemma4:e4b"
  max_tokens: 50
  temperature: 0.0
  concurrency: 2
  expected_digest: "c6eb396dbd59"
chunking:
  max_chars: 8000
  overlap_chars: 200
"""


@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return load_config(p)


def _fake_ollama(digest="c6eb396dbd59aa", ctx=8192, loaded=True):
    responses = {
        "/api/version": {"version": "0.40.13"},
        "/api/tags": {"models": [{"name": "gemma4:e4b", "digest": digest}]},
        "/api/ps": {"models": [{"name": "gemma4:e4b", "context_length": ctx}] if loaded else []},
    }
    return lambda path: responses[path]


def _by_name(checks):
    return {c.name: c for c in checks}


def test_ollama_all_good(cfg, monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "2")
    checks = _by_name(doctor.check_ollama(cfg, "http://localhost:11434", _fake_ollama(),
                                          lan_ip=lambda: "192.168.1.20", can_connect=lambda h, p: False))
    assert {c.status for c in checks.values()} == {doctor.PASS}


def test_ollama_failures(cfg, monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)
    checks = _by_name(doctor.check_ollama(cfg, "http://10.0.0.5:11434", _fake_ollama(digest="ffff00001111", ctx=4096),
                                          lan_ip=lambda: "192.168.1.20", can_connect=lambda h, p: True))
    assert checks["Ollama is local"].status == doctor.FAIL
    assert checks["Ollama not on the network"].status == doctor.FAIL
    assert checks["Model digest pinned"].status == doctor.FAIL
    assert checks["Context window"].status == doctor.FAIL
    assert checks["Parallelism"].status == doctor.WARN


def test_ollama_down_and_model_missing(cfg):
    def down(path):
        raise ConnectionError("connection refused")
    checks = _by_name(doctor.check_ollama(cfg, "http://localhost:11434", down))
    assert checks["Ollama reachable"].status == doctor.FAIL
    assert "Model installed" not in checks

    no_model = {"/api/version": {"version": "1"}, "/api/tags": {"models": []}}
    checks = _by_name(doctor.check_ollama(cfg, "http://localhost:11434", no_model.__getitem__,
                                          lan_ip=lambda: None))
    assert checks["Model installed"].status == doctor.FAIL


def test_data_dir(tmp_path: Path):
    assert doctor.check_data_dir(None)[0].status == doctor.FAIL
    case = tmp_path / "CASE-1"
    data = case / "data"
    (data / "input").mkdir(parents=True)
    (data / "input" / "export.zip").write_bytes(b"x" * 1000)
    case.chmod(0o700)
    data.chmod(0o755)
    usage = types.SimpleNamespace(free=10_000)
    checks = _by_name(doctor.check_data_dir(str(data), disk_usage=lambda p: usage))
    assert checks["DSAR_DATA_DIR"].status == doctor.PASS
    assert checks["Data dir permissions"].status == doctor.PASS   # protected by the case dir
    assert checks["Disk space"].status == doctor.PASS
    tight = types.SimpleNamespace(free=2_000)
    assert _by_name(doctor.check_data_dir(str(data), disk_usage=lambda p: tight))["Disk space"].status == doctor.WARN


def test_synced_paths_detected():
    assert doctor.is_synced_path(Path("/Users/a/Library/CloudStorage/OneDrive-Org/cases"))
    assert doctor.is_synced_path(Path("/Users/a/OneDrive - Org Ltd/cases"))
    assert doctor.is_synced_path(Path("/Users/a/Dropbox (Team)/cases"))
    assert not doctor.is_synced_path(Path("/srv/dsar/CASE-1/data"))
    # A folder merely named after a synced path is not itself synced.
    assert not doctor.is_synced_path(Path("/tmp/-Users-a-Library-CloudStorage-OneDrive-Org/data"))


@pytest.mark.parametrize("system, output, status", [
    ("Darwin", "FileVault is On.", doctor.PASS),
    ("Darwin", "FileVault is Off.", doctor.FAIL),
    ("Linux", "TYPE\ndisk\npart\ncrypt\n", doctor.PASS),
    ("Windows", "Protection Status:    Protection Off", doctor.FAIL),
    ("Darwin", "", doctor.WARN),
])
def test_encryption(system, output, status):
    assert doctor.check_encryption(system, run=lambda cmd: output).status == status


def test_ram(cfg):
    assert doctor.check_ram(cfg, 16 * doctor.GIB).status == doctor.PASS
    assert doctor.check_ram(cfg, 8 * doctor.GIB).status == doctor.WARN
    cfg.model.concurrency = 4
    assert doctor.check_ram(cfg, 16 * doctor.GIB).status == doctor.WARN


def test_lock_comparison(tmp_path: Path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("# comment\nclick==8.3.3\nhttpx==0.28.1\n", encoding="utf-8")
    versions = {"click": "8.3.3", "httpx": "0.28.1"}
    assert doctor.check_lock(lock, version=versions.__getitem__).status == doctor.PASS
    versions["httpx"] = "0.27.0"
    result = doctor.check_lock(lock, version=versions.__getitem__)
    assert result.status == doctor.WARN and "httpx 0.27.0" in result.detail


def test_shipped_lockfile_parses():
    pins = doctor.parse_lock(Path(__file__).resolve().parents[1] / "requirements.lock")
    assert "libpff-python" in pins and "libratom" not in pins


def test_case_files_placeholders(tmp_path: Path):
    config = tmp_path / "config.yaml"
    prompt = tmp_path / "claude_strict.md"
    config.write_text(CONFIG.replace("Alex Example", "<SUBJECT FULL NAME>"), encoding="utf-8")
    prompt.write_text("Subject: <ALIAS 1>", encoding="utf-8")
    checks, cfg = doctor.check_case_files(config, prompt)
    assert [c.status for c in checks] == [doctor.FAIL, doctor.FAIL]
    config.write_text(CONFIG, encoding="utf-8")
    prompt.write_text("Subject: Alex Example", encoding="utf-8")
    checks, cfg = doctor.check_case_files(config, prompt)
    assert [c.status for c in checks] == [doctor.PASS, doctor.PASS] and cfg is not None


def test_modules_reported():
    def fake_import(name):
        if name == "pypff":
            raise ImportError("no module named pypff")
    checks = _by_name(doctor.check_modules(import_module=fake_import))
    assert checks["import pypff"].status == doctor.FAIL
    assert checks["import httpx"].status == doctor.PASS


class _Folder:
    def __init__(self, name, messages=(), subs=()):
        self.name = name
        self.sub_messages = list(messages)
        self.sub_folders = list(subs)


def test_pst_walk_matches_libratom_order(monkeypatch, tmp_path: Path):
    """The pypff walk that replaced libratom must yield messages in the same order."""
    tree = _Folder("root", ["r1"], [
        _Folder("Inbox", ["i1", "i2"], [_Folder("Inbox/Sub", ["s1"])]),
        _Folder("Sent", ["t1"]),
    ])

    class FakeFile:
        root_folder = tree
        def open(self, *a): pass
        def close(self): pass

    monkeypatch.setitem(sys.modules, "pypff", types.SimpleNamespace(file=FakeFile))
    from dsar.chunking import pst
    ours = list(pst._archive_messages(tmp_path / "x.pst"))
    # libratom: deque.pop() + extendleft(sub_folders) = breadth first, siblings in order.
    assert ours == ["r1", "i1", "i2", "t1", "s1"]
