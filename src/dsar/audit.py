"""
Case audit log, written automatically by every `dsar` command.

Two append-only files under <DSAR_DATA_DIR>/output/:
  AUDIT_LOG.jsonl  machine-readable. Each entry carries the SHA-256 of the line
                   before it, so editing, removing or reordering an entry breaks
                   the chain (`dsar audit verify`).
  AUDIT_LOG.md     the human-readable record operators already keep. Entries are
                   appended below anything written by hand, and each one quotes
                   its JSONL line hash so the two can be cross-checked (which also
                   covers the newest JSONL line, which no later entry protects).

Every command writes a `start` entry (parameters, code version and commit,
config and prompt hashes) and an `end` entry (status, duration, counts, file
hashes, warnings, errors), whether it succeeds, fails or is interrupted.
"""
import getpass
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from dsar import __version__

try:  # POSIX only; on Windows concurrent writers are not locked.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


JSONL_NAME = "AUDIT_LOG.jsonl"
MD_NAME = "AUDIT_LOG.md"
PROGRESS_NAME = "progress.json"
GENESIS = "0" * 64

MD_HEADER = (
    "# Audit log\n\n"
    "Append-only. Entries headed `dsar ...` are written by the pipeline; each quotes the "
    "SHA-256 of its line in AUDIT_LOG.jsonl. Operators add notes with `dsar audit note`, "
    "or by appending at the end. Correct by a new entry, never by editing an old one.\n"
)

_MD_HASH = re.compile(r"entry #(\d+), sha256 `([0-9a-f]{64})`")
_LIST_LIMIT = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def actor() -> str:
    """Who ran the command: DSAR_OPERATOR if set, else the OS login."""
    return os.environ.get("DSAR_OPERATOR") or getpass.getuser()


def code_version() -> dict:
    """Package version plus git commit and dirty flag when running from a checkout."""
    here = Path(__file__).resolve().parent
    out: dict = {"version": __version__, "commit": "", "dirty": None}
    try:
        head = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        if head.returncode == 0:
            out["commit"] = head.stdout.strip()
            status = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=10)
            out["dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return out


@contextmanager
def _locked(path: Path) -> Iterator:
    with path.open("a+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_UN)


def _fmt(value) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        items = [_fmt(v) for v in value[:_LIST_LIMIT]]
        more = len(value) - _LIST_LIMIT
        return "; ".join(items) + (f"; ... (+{more} more)" if more > 0 else "")
    return str(value)


def render_md(entry: dict, line_hash: str) -> str:
    status = entry["data"].get("status")
    title = f"`dsar {entry['command']}` {entry['event']}" + (f" ({status})" if status else "")
    lines = [f"\n### {entry['ts']} · {title}", f"- actor: {entry['actor']} on {entry['machine']}"]
    for key, value in entry["data"].items():
        if key == "status":
            continue
        if key == "sha256" and isinstance(value, dict):
            lines += [f"- sha256 {label}: `{digest}`" for label, digest in value.items()]
            continue
        lines.append(f"- {key}: {_fmt(value)}")
    lines.append(f"- entry #{entry['seq']}, sha256 `{line_hash}`")
    return "\n".join(lines) + "\n"


class AuditLog:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.jsonl = self.output_dir / JSONL_NAME
        self.md = self.output_dir / MD_NAME

    def append(self, event: str, command: str, data: dict | None = None, run_id: str | None = None) -> dict:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with _locked(self.jsonl) as f:
            f.seek(0)
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            entry = {
                "seq": len(lines),
                "ts": _now(),
                "event": event,
                "command": command,
                "run_id": run_id,
                "actor": actor(),
                "machine": socket.gethostname(),
                "data": data or {},
                "prev": _line_hash(lines[-1]) if lines else GENESIS,
            }
            line = json.dumps(entry, sort_keys=True, default=str, ensure_ascii=False)
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
            new_md = not self.md.exists()
            with self.md.open("a", encoding="utf-8") as md:
                if new_md:
                    md.write(MD_HEADER)
                md.write(render_md(entry, _line_hash(line)))
        return entry

    def entries(self) -> list[dict]:
        if not self.jsonl.exists():
            return []
        return [json.loads(ln) for ln in self.jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def verify(self) -> tuple[int, str, list[str]]:
        """Check the hash chain and the Markdown cross-references.

        Returns (entries, head hash, problems). No problems means no entry was
        changed, removed or reordered since it was written.
        """
        if not self.jsonl.exists():
            return 0, GENESIS, [f"{self.jsonl} not found"]
        problems: list[str] = []
        hashes: dict[int, str] = {}
        prev = GENESIS
        n = 0
        for i, line in enumerate(self.jsonl.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                problems.append(f"line {i}: blank line")
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {i}: not valid JSON")
                prev = _line_hash(line)
                n += 1
                continue
            if entry.get("prev") != prev:
                problems.append(f"line {i} (entry #{entry.get('seq')}): chain broken - "
                                "the entry before it was changed, removed or reordered")
            if entry.get("seq") != n:
                problems.append(f"line {i}: expected entry #{n}, found #{entry.get('seq')}")
            prev = _line_hash(line)
            hashes[n] = prev
            n += 1
        if self.md.exists():
            for seq, digest in _MD_HASH.findall(self.md.read_text(encoding="utf-8")):
                expected = hashes.get(int(seq))
                if expected is None:
                    problems.append(f"{MD_NAME}: entry #{seq} is not in {JSONL_NAME}")
                elif expected != digest:
                    problems.append(f"{MD_NAME}: entry #{seq} hash does not match {JSONL_NAME} "
                                    "(one of the two was edited)")
        else:
            problems.append(f"{self.md} not found")
        return n, prev, problems


@dataclass
class Run:
    """One command's audit context: add counts to `data`, hash outputs, record warnings."""
    log: AuditLog
    command: str
    run_id: str
    data: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Kept apart from `data` so a command's own keys can never overwrite them.
    hashes: dict[str, str] = field(default_factory=dict)

    def hash_file(self, label: str, path: str | Path) -> None:
        p = Path(path)
        if p.is_file():
            self.hashes[label] = sha256_file(p)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


@contextmanager
def run(output_dir: Path, command: str, params: dict) -> Iterator[Run]:
    log = AuditLog(output_dir)
    r = Run(log, command, uuid.uuid4().hex[:12])
    log.append("start", command, {
        "params": params,
        "code": code_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }, r.run_id)
    t0 = time.monotonic()
    status, error = "ok", None
    try:
        yield r
    except KeyboardInterrupt:
        status, error = "interrupted", "KeyboardInterrupt"
        raise
    except BaseException as exc:
        # click signals a clean exit with Exit(0) / SystemExit(0).
        code = getattr(exc, "exit_code", getattr(exc, "code", 1))
        if code not in (0, None):
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        end = {"status": status, "duration_s": round(time.monotonic() - t0, 1), **r.data}
        if r.hashes:
            end["sha256"] = r.hashes
        if r.warnings:
            end["warnings"] = r.warnings
        if error:
            end["error"] = error
        log.append("end", command, end, r.run_id)


class Progress:
    """Keeps output/progress.json current so a long run can be checked from another shell."""

    def __init__(self, output_dir: Path, stage: str, total: int, every_s: float = 5.0):
        self.path = Path(output_dir) / PROGRESS_NAME
        self.stage = stage
        self.total = total
        self.every_s = every_s
        self.started_at = _now()
        self._t0 = time.monotonic()
        self._last = float("-inf")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def snapshot(self, done: int, note: str = "") -> dict:
        elapsed_min = (time.monotonic() - self._t0) / 60
        rate = done / elapsed_min if elapsed_min > 0 else 0.0
        remaining = max(self.total - done, 0)
        return {
            "stage": self.stage,
            "done": done,
            "total": self.total,
            "percent": round(100 * done / self.total, 1) if self.total else 100.0,
            "rate_per_min": round(rate, 2),
            "eta_min": round(remaining / rate, 1) if rate > 0 else None,
            "started_at": self.started_at,
            "updated_at": _now(),
            "note": note,
        }

    def update(self, done: int, note: str = "") -> dict | None:
        """Write a snapshot at most every `every_s` seconds (always on the last item)."""
        now = time.monotonic()
        if done < self.total and now - self._last < self.every_s:
            return None
        self._last = now
        snap = self.snapshot(done, note)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return snap
