"""
Audit entries and saved outputs for actions taken in the web app.

Case-bound: entries go to the case AUDIT_LOG (event `toolbox`, command
`toolbox <tool> <action>`) with the signed-in reviewer, and exports are saved
read-only under output/toolbox/<tool>/ with their SHA-256 in the entry.
Free-standing: nothing is written; the caller sends exports to the browser.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, session

from dsar import audit

TOOLBOX_DIR = "toolbox"


def settings():
    return current_app.config["DSAR"]


def reviewer() -> str:
    return session.get("name", "")


def log(tool: str, action: str, data: dict | None = None) -> dict | None:
    s = settings()
    if not s.case_bound:
        return None
    return audit.AuditLog(s.output_dir).append("toolbox", f"toolbox {tool} {action}",
                                               {"reviewer": reviewer(), **(data or {})})


def safe_filename(name: str, default: str = "file") -> str:
    name = Path(str(name).replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f/:*?\"<>|]+", "_", name)
    return name[:150] or default


def save_output(tool: str, filename: str, data: bytes, details: dict | None = None) -> tuple[Path, str] | None:
    """Case-bound: write `data` read-only under output/toolbox/<tool>/, log it, return (path, sha256)."""
    s = settings()
    if not s.case_bound:
        return None
    out_dir = s.output_dir / TOOLBOX_DIR / tool
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}_{safe_filename(filename)}"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"{stamp}_{n}_{safe_filename(filename)}"
    path.write_bytes(data)
    path.chmod(0o444)
    digest = audit.sha256_file(path)
    log(tool, "export", {"file": str(path), "sha256": {path.name: digest}, "bytes": len(data), **(details or {})})
    return path, digest
