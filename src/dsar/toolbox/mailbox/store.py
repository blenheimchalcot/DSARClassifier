"""
Which mailboxes the toolbox may open, and the open handles.

Case-bound (a case data dir is given): only PST / MBOX files listed in the
case's newest input manifest (`dsar manifest`) can be opened, and each file's
SHA-256 is checked against the manifest first. A file under input/ that isn't in
the manifest is listed, but can't be opened until `dsar manifest` has hashed it.

Free-standing (no case): any .pst / .mbox path the operator types in. Nothing is
checked or logged; the web app shows a banner saying so.

pypff is not thread-safe, so each open mailbox has its own lock; hold it for
every read.
"""
import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path

from dsar import audit, provenance, pst_attachments
from dsar.toolbox.mailbox import reader


def _is_mailbox_name(rel: str) -> bool:
    low = rel.lower()
    return low.endswith(".pst") or low.endswith(".mbox") or Path(low).name == "mbox"


def source_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:12]


@dataclass
class Source:
    id: str
    path: Path
    label: str                       # path relative to input/ in a case, else the full path
    size: int
    expected_sha256: str | None      # from the manifest; None when free-standing
    in_manifest: bool = True

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "size": self.size, "in_manifest": self.in_manifest,
                "kind": "mbox" if reader.is_mbox(str(self.path)) else "pst"}


@dataclass
class Opened:
    source: Source
    handle: object
    sha256: str | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    cache: dict = field(default_factory=dict)    # per-mailbox results tools want to keep (e.g. Teams index)


class MailboxError(Exception):
    pass


class MailboxStore:
    def __init__(self, data_dir: Path | None):
        self.data_dir = Path(data_dir) if data_dir else None
        self._free: dict[str, Source] = {}
        self._open: dict[str, Opened] = {}
        self._digests: dict[tuple, str] = {}     # (path, size, mtime_ns) -> sha256
        self._lock = threading.Lock()

    @property
    def case_bound(self) -> bool:
        return self.data_dir is not None

    def manifest_path(self) -> Path | None:
        if not self.data_dir:
            return None
        found = sorted((self.data_dir / "output").glob("pst_manifest_*.sha256"))
        return found[-1] if found else None

    def sources(self) -> list[Source]:
        if not self.case_bound:
            return list(self._free.values())
        input_dir = self.data_dir / "input"
        manifest = self.manifest_path()
        listed = provenance.load_manifest(manifest) if manifest else {}
        out: dict[str, Source] = {}
        for rel, digest in listed.items():
            path = input_dir / rel
            if _is_mailbox_name(rel) and path.is_file():
                out[rel] = Source(source_id(path), path, rel, path.stat().st_size, digest)
        if input_dir.is_dir():
            for path in input_dir.rglob("*"):
                rel = path.relative_to(input_dir).as_posix()
                if (rel not in out and path.is_file() and _is_mailbox_name(rel)
                        and pst_attachments.ATTACH_DIR not in path.relative_to(input_dir).parts):
                    out[rel] = Source(source_id(path), path, rel, path.stat().st_size, None, in_manifest=False)
        return sorted(out.values(), key=lambda s: s.label.casefold())

    def add_path(self, raw: str) -> Source:
        """Free-standing only: register a mailbox path the operator typed in."""
        if self.case_bound:
            raise MailboxError("in a case, only files in the input manifest can be opened")
        path = Path(raw.strip()).expanduser()
        if not path.exists():
            raise MailboxError(f"not found: {path}")
        if not (reader.is_mbox(str(path)) or path.suffix.lower() == ".pst"):
            raise MailboxError("choose a .pst file, a .mbox file, or a folder of .mbox files")
        size = path.stat().st_size if path.is_file() else 0
        src = Source(source_id(path), path, str(path), size, None)
        self._free[src.id] = src
        return src

    def find(self, sid: str) -> Source:
        for s in self.sources():
            if s.id == sid:
                return s
        raise MailboxError("unknown mailbox")

    def _digest(self, path: Path) -> str:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in self._digests:
            self._digests[key] = audit.sha256_file(path)
        return self._digests[key]

    def open(self, sid: str) -> tuple[Opened, bool]:
        """Open (or return the already-open) mailbox. Returns (opened, newly_opened)."""
        with self._lock:
            if sid in self._open:
                return self._open[sid], False
            src = self.find(sid)
            sha = None
            if self.case_bound:
                if not src.in_manifest or not src.expected_sha256:
                    raise MailboxError(f"{src.label} is not in the input manifest; run `dsar manifest` first")
                sha = self._digest(src.path)
                if sha != src.expected_sha256:
                    raise MailboxError(f"{src.label} no longer matches its manifest hash; not opened")
            try:
                handle = reader.open_mailbox(str(src.path))
            except Exception as exc:
                raise MailboxError(f"could not open {src.label}: {exc}") from exc
            opened = Opened(src, handle, sha)
            self._open[sid] = opened
            return opened, True

    def get(self, sid: str) -> Opened:
        opened = self._open.get(sid)
        if opened is None:
            raise MailboxError("mailbox not open")
        return opened

    def close_all(self) -> None:
        with self._lock:
            for o in self._open.values():
                try:
                    o.handle.close()
                except Exception:
                    pass
            self._open.clear()
