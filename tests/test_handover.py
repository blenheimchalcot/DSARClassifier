import stat
from pathlib import Path

from click.testing import CliRunner

from dsar import __version__, audit
from dsar.cli import main


def test_version():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0 and result.output.startswith(f"dsar {__version__}")


def test_init_case(tmp_path: Path):
    root = tmp_path / "cases"
    env = tmp_path / ".env"
    runner = CliRunner()
    result = runner.invoke(main, ["init-case", "CASE-001", "--root", str(root), "--controller", "Example Ltd",
                                  "--env-file", str(env)], env={"DSAR_OPERATOR": "Op One"})
    assert result.exit_code == 0, result.output
    data = root / "CASE-001" / "data"
    assert (data / "input").is_dir() and (data / "output").is_dir()
    assert stat.S_IMODE((root / "CASE-001").stat().st_mode) == 0o700
    assert (data / "config.yaml").read_text().startswith("# TEMPLATE")
    assert (data / "claude_strict.md").exists()
    assert env.read_text() == f"DSAR_DATA_DIR={data}\n"

    entry = audit.AuditLog(data / "output").entries()[0]
    assert entry["command"] == "init-case" and entry["actor"] == "Op One"
    assert entry["data"]["controller"] == "Example Ltd" and entry["data"]["case_ref"] == "CASE-001"

    # Never over an existing case; an existing .env is left alone.
    again = runner.invoke(main, ["init-case", "CASE-001", "--root", str(root), "--env-file", str(env)])
    assert again.exit_code != 0 and "already exists" in again.output
    other = runner.invoke(main, ["init-case", "CASE-002", "--root", str(root), "--env-file", str(env)])
    assert other.exit_code == 0 and "not changed" in other.output
    assert env.read_text() == f"DSAR_DATA_DIR={data}\n"


def test_init_case_refuses_synced_and_bad_refs(tmp_path: Path):
    runner = CliRunner()
    synced = tmp_path / "OneDrive - Example Ltd" / "cases"
    result = runner.invoke(main, ["init-case", "CASE-1", "--root", str(synced), "--env-file", str(tmp_path / ".env")])
    assert result.exit_code != 0 and "cloud-synced" in result.output
    assert not synced.exists()
    result = runner.invoke(main, ["init-case", "../escape", "--root", str(tmp_path), "--env-file", str(tmp_path / ".env")])
    assert result.exit_code != 0
