"""
`dsar doctor`: check a machine is set up to run a case, before any case data is touched.

Each check returns PASS, WARN or FAIL with a one-line reason. FAIL means a case
must not run on this machine as it stands; WARN means look at it and decide.
The checks mirror DEPLOYMENT.md and OPERATIONS.md §3, where most of them come
from something that went wrong on a real run.
"""
import importlib
import importlib.metadata
import ipaddress
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from dsar.config import Config

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# Module -> what it is for. All are needed by some ingest path.
RUNTIME_MODULES = {
    "pypff": "PST reader (libpff-python)",
    "docx": "Word documents",
    "openpyxl": "Excel .xlsx",
    "xlrd": "Excel .xls",
    "pyxlsb": "Excel .xlsb",
    "pptx": "PowerPoint",
    "pypdf": "PDF",
    "extract_msg": "Outlook .msg",
    "bs4": "HTML",
    "striprtf": "RTF bodies",
    "yaml": "config",
    "dotenv": ".env",
    "click": "CLI",
    "httpx": "Ollama client",
    "flask": "web app and toolbox (dsar serve)",
    "reportlab": "Teams transcript PDFs",
}

MIN_CONTEXT_TOKENS = 8192
GIB = 1024 ** 3

# Folder names (lower case) that are cloud-synced roots. Case data must never sit under one.
# Matched per path component: e.g. "OneDrive - Org", "Dropbox (Team)", ~/Library/CloudStorage.
SYNCED_PREFIXES = ("onedrive", "dropbox", "google drive", "icloud drive", "box sync")
SYNCED_NAMES = {"cloudstorage", "mobile documents", "box"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 10)
    return Check("Python", PASS if ok else FAIL, f"{platform.python_version()} (3.10+ required)")


def check_modules(import_module: Callable = importlib.import_module) -> list[Check]:
    out = []
    for module, purpose in RUNTIME_MODULES.items():
        try:
            import_module(module)
            out.append(Check(f"import {module}", PASS, purpose))
        except Exception as exc:
            out.append(Check(f"import {module}", FAIL, f"{purpose}: {exc}"))
    return out


def parse_lock(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, _, version = line.partition("==")
            pins[name.strip()] = version.strip()
    return pins


def check_lock(lock_path: Path | None, version: Callable = importlib.metadata.version) -> Check:
    if lock_path is None or not lock_path.exists():
        return Check("Package versions", WARN, "requirements.lock not found; cannot compare versions")
    differ, missing = [], []
    for name, pinned in parse_lock(lock_path).items():
        try:
            installed = version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
            continue
        if installed != pinned:
            differ.append(f"{name} {installed} (lock {pinned})")
    if not differ and not missing:
        return Check("Package versions", PASS, f"match {lock_path.name}")
    parts = []
    if differ:
        parts.append("differ: " + ", ".join(differ))
    if missing:
        parts.append("not installed: " + ", ".join(missing))
    return Check("Package versions", WARN, "; ".join(parts) + " (pip install -r requirements.lock)")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _lan_ip() -> str | None:
    """This machine's address on its default route (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        return None if ipaddress.ip_address(ip).is_loopback else ip
    except OSError:
        return None


def _can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def check_ollama(cfg: Config | None, base_url: str, get: Callable[[str], dict],
                 lan_ip: Callable[[], str | None] = _lan_ip,
                 can_connect: Callable[[str, int], bool] = _can_connect) -> list[Check]:
    """`get(path)` returns the JSON body of GET base_url+path, or raises."""
    out = []
    parsed = urlparse(base_url)
    host, port = parsed.hostname or "", parsed.port or 11434
    if _is_loopback(host):
        out.append(Check("Ollama is local", PASS, base_url))
    else:
        out.append(Check("Ollama is local", FAIL,
                         f"{base_url} is not this machine: case text would leave it (set OLLAMA_URL to localhost)"))

    try:
        version = get("/api/version").get("version")
        out.append(Check("Ollama reachable", PASS, f"version {version}"))
    except Exception as exc:
        out.append(Check("Ollama reachable", FAIL, f"{base_url}: {exc} (start it: see OPERATIONS §3)"))
        return out

    ip = lan_ip()
    if ip is None:
        out.append(Check("Ollama not on the network", WARN, "could not find this machine's network address to test"))
    elif can_connect(ip, port):
        out.append(Check("Ollama not on the network", FAIL,
                         f"reachable at {ip}:{port}; bind it to 127.0.0.1 (unset OLLAMA_HOST=0.0.0.0)"))
    else:
        out.append(Check("Ollama not on the network", PASS, f"{ip}:{port} refused"))

    if cfg is None:
        return out
    model = cfg.model.default
    names = {model, f"{model}:latest"}
    try:
        tags = get("/api/tags").get("models", [])
    except Exception as exc:
        out.append(Check("Model installed", FAIL, f"could not list models: {exc}"))
        return out
    found = next((m for m in tags if m.get("name") in names or m.get("model") in names), None)
    if found is None:
        out.append(Check("Model installed", FAIL, f"{model} not found (ollama pull {model})"))
        return out
    digest = found.get("digest") or ""
    out.append(Check("Model installed", PASS, f"{model} digest {digest[:12]}"))

    expected = (cfg.model.expected_digest or "").lower()
    if not expected:
        out.append(Check("Model digest pinned", WARN,
                         f"model.expected_digest not set in config; installed build is {digest[:12]}"))
    elif digest.lower().startswith(expected):
        out.append(Check("Model digest pinned", PASS, f"matches {expected}"))
    else:
        out.append(Check("Model digest pinned", FAIL,
                         f"installed {digest[:12]} but config expects {expected}: a different model build"))

    try:
        loaded = next((m for m in get("/api/ps").get("models", [])
                       if m.get("name") in names or m.get("model") in names), None)
    except Exception:
        loaded = None
    ctx = (loaded or {}).get("context_length")
    if ctx is None:
        out.append(Check("Context window", WARN,
                         f"model not loaded, cannot read it; start Ollama with OLLAMA_CONTEXT_LENGTH={MIN_CONTEXT_TOKENS} "
                         "(`dsar model-check` tests for truncation)"))
    elif ctx < MIN_CONTEXT_TOKENS:
        out.append(Check("Context window", FAIL,
                         f"{ctx} tokens: {cfg.chunking.max_chars}-char chunks will be truncated "
                         f"(OLLAMA_CONTEXT_LENGTH={MIN_CONTEXT_TOKENS})"))
    else:
        out.append(Check("Context window", PASS, f"{ctx} tokens"))

    parallel = os.environ.get("OLLAMA_NUM_PARALLEL")
    if parallel is None:
        out.append(Check("Parallelism", WARN,
                         f"cannot read the server's OLLAMA_NUM_PARALLEL; it must equal model.concurrency "
                         f"({cfg.model.concurrency})"))
    elif parallel.strip() != str(cfg.model.concurrency):
        out.append(Check("Parallelism", WARN,
                         f"OLLAMA_NUM_PARALLEL={parallel} here but model.concurrency={cfg.model.concurrency}"))
    else:
        out.append(Check("Parallelism", PASS, f"{parallel}"))
    return out


def is_synced_path(path: Path) -> bool:
    for part in path.expanduser().resolve().parts:
        low = part.lower()
        if low in SYNCED_NAMES or low.startswith(SYNCED_PREFIXES):
            return True
    return False


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def check_data_dir(raw: str | None, disk_usage: Callable = shutil.disk_usage) -> list[Check]:
    if not raw:
        return [Check("DSAR_DATA_DIR", FAIL, "not set (add it to .env)")]
    data_dir = Path(raw).expanduser()
    if not data_dir.is_dir():
        return [Check("DSAR_DATA_DIR", FAIL, f"{data_dir} does not exist")]
    out = [Check("DSAR_DATA_DIR", PASS, str(data_dir))]
    if is_synced_path(data_dir):
        out.append(Check("Data dir not synced", FAIL, f"{data_dir} is inside a cloud-synced folder"))
    else:
        out.append(Check("Data dir not synced", PASS, "not under a known cloud-synced folder"))
    if os.name == "posix":
        mode = data_dir.stat().st_mode & 0o777
        if mode & 0o077:
            # Case dirs are chmod 700 so other accounts on the machine can't read them.
            parent = data_dir.parent
            parent_mode = parent.stat().st_mode & 0o777
            if parent_mode & 0o077:
                out.append(Check("Data dir permissions", WARN,
                                 f"{oct(mode)} on {data_dir.name} and {oct(parent_mode)} on its case dir; use chmod 700"))
            else:
                out.append(Check("Data dir permissions", PASS, f"case dir {parent.name} is {oct(parent_mode)}"))
        else:
            out.append(Check("Data dir permissions", PASS, oct(mode)))
    free = disk_usage(data_dir).free
    input_dir = data_dir / "input"
    export = _dir_size(input_dir) if input_dir.is_dir() else 0
    need = 3 * export
    detail = f"{free / GIB:.1f} GiB free; input/ is {export / GIB:.1f} GiB"
    if export and free < export:
        out.append(Check("Disk space", FAIL, detail + " (need ~3x the export)"))
    elif export and free < need:
        out.append(Check("Disk space", WARN, detail + " (need ~3x the export)"))
    else:
        out.append(Check("Disk space", PASS, detail))
    return out


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.stdout + r.stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def check_encryption(system: str | None = None, run: Callable[[list[str]], str] = _run) -> Check:
    system = system or platform.system()
    if system == "Darwin":
        out = run(["fdesetup", "status"])
        if "FileVault is On" in out:
            return Check("Disk encryption", PASS, "FileVault on")
        if "FileVault is Off" in out:
            return Check("Disk encryption", FAIL, "FileVault off: turn it on before holding case data")
    elif system == "Linux":
        out = run(["lsblk", "-o", "TYPE"])
        if re.search(r"^\s*crypt\s*$", out, re.MULTILINE):
            return Check("Disk encryption", PASS, "LUKS volume present (check case data is on it)")
    elif system == "Windows":
        out = run(["manage-bde", "-status", os.environ.get("SystemDrive", "C:")])
        if re.search(r"Protection Status:\s*Protection On", out):
            return Check("Disk encryption", PASS, "BitLocker on")
        if re.search(r"Protection Status:\s*Protection Off", out):
            return Check("Disk encryption", FAIL, "BitLocker off")
    return Check("Disk encryption", WARN, "could not confirm full-disk encryption; check it by hand")


def total_ram() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def check_ram(cfg: Config | None, ram: int | None) -> Check:
    if ram is None:
        return Check("Memory", WARN, "could not read installed RAM")
    gib = ram / GIB
    if gib < 15:
        return Check("Memory", WARN, f"{gib:.0f} GiB; 16 GiB is the minimum for gemma4:e4b")
    if cfg and cfg.model.concurrency > 2 and gib < 30:
        return Check("Memory", WARN, f"{gib:.0f} GiB with concurrency {cfg.model.concurrency}; "
                                     "above 2 needs 32 GiB (OPERATIONS §3)")
    return Check("Memory", PASS, f"{gib:.0f} GiB")


_PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9 ,_.-]+>")


def check_case_files(config_path: Path, prompt_path: Path) -> tuple[list[Check], Config | None]:
    from dsar.config import load_config

    out, cfg = [], None
    if not config_path.exists():
        out.append(Check("Case config", FAIL, f"{config_path} not found (copy config.example.yaml)"))
    else:
        try:
            cfg = load_config(config_path)
            if _PLACEHOLDER.search(config_path.read_text(encoding="utf-8")):
                out.append(Check("Case config", FAIL, f"{config_path} still has <PLACEHOLDER> values"))
            else:
                out.append(Check("Case config", PASS, f"{config_path} ({len(cfg.subject.aliases)} aliases)"))
        except Exception as exc:
            out.append(Check("Case config", FAIL, f"{config_path}: {exc}"))
    if not prompt_path.exists():
        out.append(Check("System prompt", FAIL, f"{prompt_path} not found (copy claude_strict.example.md)"))
    elif _PLACEHOLDER.search(prompt_path.read_text(encoding="utf-8")):
        out.append(Check("System prompt", FAIL, f"{prompt_path} still has <PLACEHOLDER> values"))
    else:
        out.append(Check("System prompt", PASS, str(prompt_path)))
    return out, cfg


def default_lock_path() -> Path | None:
    # src/dsar/doctor.py -> repo root, when running from a checkout.
    p = Path(__file__).resolve().parents[2] / "requirements.lock"
    return p if p.exists() else None


def run_all(config_path: Path, prompt_path: Path, data_dir: str | None, ollama_url: str,
            get: Callable[[str], dict]) -> list[Check]:
    checks = [check_python(), *check_modules(), check_lock(default_lock_path())]
    case_checks, cfg = check_case_files(config_path, prompt_path)
    checks += case_checks
    checks += check_ollama(cfg, ollama_url, get)
    checks += check_data_dir(data_dir)
    checks.append(check_encryption())
    checks.append(check_ram(cfg, total_ram()))
    return checks
