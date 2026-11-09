import asyncio
import gc
import os
import time
from pathlib import Path

import click
from dotenv import load_dotenv

from dsar import db, provenance, pst_attachments, report
from dsar.chunking import (
    csv_table,
    documents,
    eml_message,
    generic_text,
    html_transcript,
    outlook_msg,
    pptx_deck,
    pst,
    spreadsheets,
    substrate_hold,
    teams_chat,
)
from dsar.chunking.prefilter import build_matcher
from dsar.classify import classify_all, load_system_prompt
from dsar.config import load_config


DEFAULT_CONFIG = "config.yaml"
# claude_strict.md is the production system prompt — it closes the
# "[not able to classify]" loophole and bakes in the NDR/notification examples.
# claude.md is retained as the historical reference cited in methodology.md.
DEFAULT_SYSTEM_PROMPT = "claude_strict.md"

# Path-component exclusions: anything under these directory names is excluded
# from the walker entirely. The skipped table receives one summary entry per
# excluded subtree, not one per file (which would otherwise add ~100k rows of
# Python virtualenv noise to the audit table — see methodology Appendix B).
EXCLUDED_PATH_PARTS = frozenset({
    ".venv", "venv", "env",                    # Python virtualenvs
    "site-packages", "__pycache__",            # Python install/cache dirs
    "node_modules",                            # Node.js dependencies
    ".idea", ".vscode",                        # IDE config
    "__MACOSX",                                # AppleDouble metadata stubs from zip extracts
})

# When someone saves an Office file (xlsx/pptx/docx) on SharePoint with a `.zip`
# extension, our recursive extractor unzips it as if it were a regular archive.
# The result is the Office package's *internal* XML parts appearing as standalone
# files. These have no narrative PI on their own and would otherwise generate
# thousands of alias-miss chunks. We skip them and record one per Office-package
# root in the audit table.
OFFICE_INTERNAL_DIRS = frozenset({"_rels", "xl", "word", "ppt", "docProps", "customXml", "theme"})


def _is_office_internal(path: Path) -> bool:
    parts = path.parts
    for i, part in enumerate(parts):
        if part.endswith(".unzipped") and i + 1 < len(parts) and parts[i + 1] in OFFICE_INTERNAL_DIRS:
            return True
    return False

# Extensions that have no narrative text content worth classifying. The skipped
# table records a meaningful reason per file so a reviewer auditing the corpus
# can see *why* something was not classified.
BINARY_NOISE_EXTS: dict[str, str] = {
    # Images
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".tif": "image", ".tiff": "image", ".ico": "image",
    ".svg": "image (SVG vector graphic — no narrative)",
    ".cr2": "Canon RAW photo",
    # Audio / video
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".aac": "audio",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".wmv": "video", ".mkv": "video",
    # Fonts
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
    ".eot": "font", ".afm": "font",
    # Web styling / templating (SCORM packaging artifacts)
    ".css": "stylesheet (no narrative content)",
    ".js": "javascript (no narrative content)",
    ".xsd": "XML schema (no narrative content)",
    ".rels": "Office relationship metadata (no narrative content)",
    ".aspx": "SharePoint page stub (no narrative content)",
    # Compiled / binary
    ".exe": "Windows executable",
    ".dll": "Windows DLL",
    ".so": "shared library",
    ".dylib": "macOS dynamic library",
    ".lib": "static library",
    ".pyc": "compiled Python bytecode",
    ".pyd": "compiled Python extension",
    ".pyi": "Python type stub",
    ".pyx": "Cython source", ".pxd": "Cython header", ".pxi": "Cython include", ".pyf": "f2py interface",
    ".typed": "PEP 561 marker",
    ".mo": "compiled gettext catalog",
    ".tpl": "template file (build artifact)",
    ".toc": "table-of-contents build artifact",
    ".tab": "build artifact",
    ".proto": "Protocol Buffers schema",
    ".f": "FORTRAN source", ".f90": "FORTRAN source", ".h": "C header",
    ".hpp": "C++ header", ".cc": "C++ source", ".cpp": "C++ source", ".c": "C source",
    ".bin": "binary blob",
    ".pbix": "compiled Power BI report (no narrative)",
    ".npy": "NumPy binary array",
    ".parquet": "Parquet binary data",
    ".gz": "gzipped archive (recursive extraction skipped)",
    # Office/runtime aux files
    ".edge": "Microsoft Edge profile data",
    ".ods": "OpenDocument spreadsheet (not supported by current pipeline)",
    ".one": "OneNote notebook (documented in methodology §8 as known gap)",
}


def _data_dir() -> Path:
    # Resolved per-call so .env (loaded in main()) is honoured. DSAR data is kept
    # outside the working tree so a OneDrive-synced repo never holds subject content.
    raw = os.environ.get("DSAR_DATA_DIR")
    if not raw:
        raise click.ClickException(
            "DSAR_DATA_DIR is not set. Add it to .env, e.g. "
            "DSAR_DATA_DIR=/Users/you/DSARClassifier/data"
        )
    return Path(raw).expanduser()


def _is_excluded_path(path: Path) -> str | None:
    """Return the excluded part name if any path component is in the exclusion list, else None."""
    for part in path.parts:
        if part in EXCLUDED_PATH_PARTS:
            return part
    return None


@click.group()
def main() -> None:
    """DSAR classification pipeline."""
    load_dotenv()


@main.command()
@click.argument("input_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), required=False)
@click.option("--db-path", default=None, help="SQLite path. Defaults to $DSAR_DATA_DIR/dsar.sqlite.")
@click.option("--config", "config_path", default=DEFAULT_CONFIG, show_default=True)
def ingest(input_dir: Path | None, db_path: str | None, config_path: str) -> None:
    """Walk INPUT_DIR (default: $DSAR_DATA_DIR/input), chunk every supported file, write to SQLite."""
    data_dir = _data_dir()
    if input_dir is None:
        input_dir = data_dir / "input"
        if not input_dir.is_dir():
            raise click.ClickException(f"Input dir not found: {input_dir}")
    if db_path is None:
        db_path = str(data_dir / "dsar.sqlite")
    cfg = load_config(config_path)
    matcher = build_matcher(cfg.subject)

    counters = {
        "email": 0, "document": 0, "spreadsheet_row": 0,
        "csv_row": 0, "pptx_slide": 0,
        "json_document": 0, "xml_document": 0, "html_document": 0,
        "skipped_files": 0, "excluded_path_files": 0,
        "alias_matched": 0, "duplicates_skipped": 0,
    }

    pst_paths: list[Path] = []
    other_paths: list[Path] = []
    excluded_roots_seen: set[tuple[str, str]] = set()
    office_internal_roots_seen: set[str] = set()

    for path in sorted(p for p in input_dir.rglob("*") if p.is_file()):
        excluded_part = _is_excluded_path(path)
        if excluded_part is not None:
            # Don't add per-file entries — that would bloat the skipped table.
            # Instead, record one entry per *ancestor* directory that triggered exclusion.
            counters["excluded_path_files"] += 1
            ancestor = path
            while ancestor.name != excluded_part and ancestor.parent != ancestor:
                ancestor = ancestor.parent
            excluded_roots_seen.add((str(ancestor), excluded_part))
            continue
        if _is_office_internal(path):
            counters["excluded_path_files"] += 1
            # Record the .unzipped root once
            ancestor = path
            while not ancestor.name.endswith(".unzipped") and ancestor.parent != ancestor:
                ancestor = ancestor.parent
            office_internal_roots_seen.add(str(ancestor))
            continue
        (pst_paths if path.suffix.lower() == ".pst" else other_paths).append(path)

    # Periodic commits so a crash mid-ingest doesn't lose the whole run.
    COMMIT_EVERY = 500

    with db.connect(db_path) as conn:
        db.init_schema(conn)

        # Resumability optimisation: if a file was successfully ingested on a
        # prior run its source_path is already in the chunks table. Skip those
        # — re-parsing them would only produce idempotent upserts but wastes
        # hours of CPU on the SharePoint xlsx-heavy corpus. The pipeline
        # commits per-file, so existence in the chunks table is a reliable
        # "this file completed" signal.
        prior_paths: set[str] = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT source_path FROM chunks WHERE source_path LIKE ?",
                (str(input_dir) + "%",),
            )
        }
        if prior_paths:
            kept = [p for p in other_paths if str(p) not in prior_paths]
            click.echo(
                f"Resume: {len(prior_paths)} files already ingested from prior run; "
                f"{len(kept)}/{len(other_paths)} files remaining to process."
            )
            other_paths = kept

        # Record one row per excluded subtree (e.g. one row for each .venv/ found).
        for root, why in sorted(excluded_roots_seen):
            db.record_skipped(
                conn, root,
                f"excluded path: '{why}' subtree (audit-bulk-skipped)",
            )
        for root in sorted(office_internal_roots_seen):
            db.record_skipped(
                conn, root,
                "Office Open XML internal parts (parent zip was an xlsx/pptx/docx renamed to .zip; "
                "internal _rels/xl/word/ppt/docProps/customXml/theme parts skipped — they contain no narrative PI)",
            )

        written_since_commit = 0

        def _maybe_commit() -> None:
            nonlocal written_since_commit
            if written_since_commit >= COMMIT_EVERY:
                conn.commit()
                written_since_commit = 0

        # Emails first: thread grouping + paragraph dedupe runs across all PSTs.
        if pst_paths:
            try:
                for chunk in pst.ingest_emails(
                    pst.iter_messages(pst_paths),
                    matcher,
                    conn,
                    max_chars=cfg.chunking.max_chars,
                    overlap_chars=cfg.chunking.overlap_chars,
                ):
                    db.upsert_chunk(conn, chunk)
                    counters[chunk.source_type] = counters.get(chunk.source_type, 0) + 1
                    if chunk.alias_matched:
                        counters["alias_matched"] += 1
                    if chunk.skipped_as_duplicate_of:
                        counters["duplicates_skipped"] += 1
                    written_since_commit += 1
                    _maybe_commit()
            except Exception as exc:
                db.record_skipped(conn, ",".join(str(p) for p in pst_paths), f"pst ingest error: {exc}")
                counters["skipped_files"] += len(pst_paths)
                click.echo(f"  ! error during PST ingest: {exc}", err=True)
            conn.commit()
            written_since_commit = 0

        # Then everything else, one file at a time.
        total_other = len(other_paths)
        t_started = time.time()
        for file_idx, path in enumerate(other_paths, start=1):
            ext = path.suffix.lower()
            parts = set(path.parts)
            # Per-file progress so a long run is observable and a hang/OOM
            # leaves a breadcrumb in the log identifying the file in flight.
            sz = path.stat().st_size if path.exists() else 0
            if file_idx % 50 == 0 or file_idx <= 10 or sz > 10 * 1024 * 1024:
                elapsed = time.time() - t_started
                rate = file_idx / elapsed if elapsed > 0 else 0
                eta_min = (total_other - file_idx) / rate / 60 if rate > 0 else -1
                click.echo(
                    f"  [{file_idx}/{total_other} {file_idx/total_other:.1%}, "
                    f"{rate:.1f} files/s, ETA {eta_min:.0f}m] "
                    f"{sz//1024}KB  {path}",
                    err=True,
                )
            # Free memory from any cyclic references the previous file left behind.
            if file_idx % 100 == 0:
                gc.collect()
            try:
                # ---- Purview-Exchange-specific paths (existing) ----
                if ext == ".xlsx" or ext in spreadsheets.SUPPORTED_EXTS:
                    stream = spreadsheets.extract(path, matcher)
                elif ext == ".json" and "SubstrateHolds" in parts:
                    stream = substrate_hold.extract(path, matcher)
                elif ext == ".json" and "SPOOLS" in parts:
                    stream = teams_chat.extract(path, matcher,
                                                cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                elif ext == ".html" and "MicrosoftMeetingTranscripts" in parts:
                    stream = html_transcript.extract(path, matcher,
                                                     cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                # ---- New chunkers for SharePoint pass ----
                elif ext == ".csv":
                    stream = csv_table.extract(path, matcher)
                elif ext == ".pptx":
                    stream = pptx_deck.extract(path, matcher,
                                               cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                elif ext == ".msg":
                    stream = outlook_msg.extract(path, matcher,
                                                 cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                elif ext == ".eml":
                    stream = eml_message.extract(path, matcher,
                                                 cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                elif ext == ".bin" and pst_attachments.ATTACH_DIR in parts and "_embedded_" in path.name:
                    db.record_skipped(conn, str(path),
                                      "raw embedded PST item, preserved; its recovered text is ingested from the sibling .txt")
                    counters["skipped_files"] += 1
                    continue
                elif ext in {".json", ".xml", ".htm", ".html"}:
                    stream = generic_text.extract(path, matcher,
                                                  cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                elif ext in documents.SUPPORTED_EXTS:
                    stream = documents.extract(path, matcher,
                                               cfg.chunking.max_chars, cfg.chunking.overlap_chars)
                # ---- Explicit noise skips (documented reason) ----
                elif ext in BINARY_NOISE_EXTS:
                    db.record_skipped(conn, str(path), f"skipped: {BINARY_NOISE_EXTS[ext]}")
                    counters["skipped_files"] += 1
                    continue
                else:
                    db.record_skipped(conn, str(path), f"unsupported extension {ext}")
                    counters["skipped_files"] += 1
                    continue

                for chunk in stream:
                    db.upsert_chunk(conn, chunk)
                    counters[chunk.source_type] = counters.get(chunk.source_type, 0) + 1
                    if chunk.alias_matched:
                        counters["alias_matched"] += 1
                    written_since_commit += 1
                    _maybe_commit()
            except Exception as exc:
                db.record_skipped(conn, str(path), f"error: {exc}")
                counters["skipped_files"] += 1
                click.echo(f"  ! error on {path}: {exc}", err=True)
            # Commit at file boundaries too — so a per-file failure later in the
            # batch can't roll back already-completed files.
            conn.commit()
            written_since_commit = 0

    click.echo("Ingest complete:")
    for k, v in counters.items():
        click.echo(f"  {k}: {v}")


@main.command()
@click.option("--db-path", default=None, help="SQLite path. Defaults to $DSAR_DATA_DIR/dsar.sqlite.")
@click.option("--config", "config_path", default=DEFAULT_CONFIG, show_default=True)
@click.option("--system-prompt", default=DEFAULT_SYSTEM_PROMPT, show_default=True)
@click.option("--model", default=None, help="Override model from config.yaml.")
@click.option("--limit", type=int, default=None, help="Classify at most N chunks (for dry-runs).")
@click.option("--plan", is_flag=True, help="Print in-scope / out-of-scope counts per provenance label and exit.")
def classify(db_path: str | None, config_path: str, system_prompt: str, model: str | None,
             limit: int | None, plan: bool) -> None:
    """Classify every alias-matched, in-scope chunk that hasn't been classified yet."""
    data_dir = _data_dir()
    if db_path is None:
        db_path = str(data_dir / "dsar.sqlite")
    cfg = load_config(config_path)
    sys_prompt = load_system_prompt(system_prompt)
    chosen_model = model or cfg.model.default

    with db.connect(db_path) as conn:
        db.init_schema(conn)
        try:
            in_scope = provenance.scope_filter(conn, data_dir / "input", cfg.classify_scope)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        if plan:
            rows = conn.execute("""
                SELECT c.source_path, COALESCE(pf.provenance_label, '(no provenance)'),
                       cl.chunk_id IS NOT NULL
                FROM chunks c
                LEFT JOIN provenance_files pf ON pf.source_path = c.source_path
                LEFT JOIN classifications cl ON cl.chunk_id = c.chunk_id
                WHERE c.alias_matched = 1 AND c.skipped_as_duplicate_of IS NULL
            """).fetchall()
            tally: dict[str, list[int]] = {}
            for source_path, label, done in rows:
                t = tally.setdefault(label, [0, 0, 0])
                t[0 if in_scope(source_path) else 1] += 1
                t[2] += done
            click.echo(f"{'in scope':>9} {'out':>9} {'done':>7}  provenance")
            for label, (i, o, d) in sorted(tally.items(), key=lambda kv: -kv[1][0]):
                click.echo(f"{i:>9} {o:>9} {d:>7}  {label}")
            click.echo(f"{sum(t[0] for t in tally.values()):>9} {sum(t[1] for t in tally.values()):>9} "
                       f"{sum(t[2] for t in tally.values()):>7}  TOTAL")
            return
    scope_desc = "all alias-matched chunks" if cfg.classify_scope.is_unrestricted else str(cfg.classify_scope)
    click.echo(f"Scope: {scope_desc}")

    def _progress(chunk_id: str, parsed, totals) -> None:
        label = parsed.special_return or f"score={parsed.score}"
        click.echo(
            f"  [{totals.requests}] {chunk_id} -> {label} "
            f"(in={totals.input_tokens} out={totals.output_tokens})"
        )

    with db.connect(db_path) as conn:
        db.init_schema(conn)
        totals = asyncio.run(classify_all(
            conn,
            system_prompt=sys_prompt,
            model=chosen_model,
            subject_name=cfg.subject.name,
            max_tokens=cfg.model.max_tokens,
            temperature=cfg.model.temperature,
            concurrency=cfg.model.concurrency,
            limit=limit,
            progress=_progress,
            in_scope=in_scope,
        ))

    click.echo("Classification complete:")
    click.echo(f"  requests:      {totals.requests}")
    click.echo(f"  input_tokens:  {totals.input_tokens}")
    click.echo(f"  output_tokens: {totals.output_tokens}")


@main.command(name="attachments")
@click.option("--manifest", "manifest_path", default=None,
              help="SHA-256 manifest to append stage-4 hashes to. Defaults to the newest output/pst_manifest_*.sha256.")
def attachments_cmd(manifest_path: str | None) -> None:
    """Extract every PST attachment into input/PST_attachments/ and catalogue it against its parent email.

    Run after the PSTs are hashed and before `dsar ingest`, which then chunks
    the extracted files like any other input file.
    """
    data_dir = _data_dir()
    input_dir = data_dir / "input"
    pst_paths = sorted(p for p in input_dir.rglob("*.pst")
                       if pst_attachments.ATTACH_DIR not in p.relative_to(input_dir).parts)
    if not pst_paths:
        raise click.ClickException(f"No .pst files under {input_dir}")
    rows = pst_attachments.extract(pst_paths, input_dir)
    catalogue = data_dir / "output" / pst_attachments.CATALOGUE_NAME
    pst_attachments.write_catalogue(rows, catalogue)

    stored = sorted({rel for r in rows for rel in (r.get("stored_rel_path"), r.get("recovered_text_rel_path")) if rel})
    if manifest_path is None:
        manifests = sorted((data_dir / "output").glob("pst_manifest_*.sha256"))
        manifest_path = str(manifests[-1]) if manifests else None
    if manifest_path:
        # Append-only: stage 4 lists the extracted files so `dsar provenance`
        # can verify them like every other input file.
        known = provenance.load_manifest(Path(manifest_path))
        new = [rel for rel in stored if rel not in known]
        if new:
            with open(manifest_path, "a", encoding="utf-8") as f:
                f.write(f"# Stage 4: PST attachments extracted by `dsar attachments`. Hashed {db._now()}\n")
                for rel in new:
                    f.write(f"{provenance.sha256_file(input_dir / rel)}  {rel}\n")
        click.echo(f"Manifest: {manifest_path} (+{len(new)} stage-4 entries)")

    by_method: dict[str, int] = {}
    for r in rows:
        key = {1: "file", 5: "embedded item"}.get(r.get("attach_method"), f"method {r.get('attach_method')}")
        by_method[key] = by_method.get(key, 0) + 1
    click.echo("Attachments complete:")
    click.echo(f"  PSTs:                 {len(pst_paths)}")
    click.echo(f"  attachment instances: {len(rows)}  {by_method}")
    click.echo(f"  distinct (stored):    {sum(1 for r in rows if r.get('first_instance') == 'yes')}")
    click.echo(f"  unreadable:           {sum(1 for r in rows if (r.get('note') or '').startswith('unreadable'))}")
    click.echo(f"  needs manual review:  {sum(1 for r in rows if (r.get('note') or '').startswith('embedded email'))}"
               " (embedded emails: body not recoverable via pypff)")
    click.echo(f"  files written:        {len(stored)} (incl. recovered-text files)")
    click.echo(f"Catalogue: {catalogue}")


@main.command(name="provenance")
@click.option("--db-path", default=None, help="SQLite path. Defaults to $DSAR_DATA_DIR/dsar.sqlite.")
@click.option("--config", "config_path", default=DEFAULT_CONFIG, show_default=True)
@click.option("--manifest", "manifest_path", default=None,
              help="SHA-256 manifest to verify against. Defaults to the newest output/pst_manifest_*.sha256.")
@click.option("--out", "out_path", default=None,
              help="Catalogue CSV path. Defaults to $DSAR_DATA_DIR/output/provenance_catalogue.csv.")
def provenance_cmd(db_path: str | None, config_path: str, manifest_path: str | None, out_path: str | None) -> None:
    """Catalogue where every input file came from (hash, Purview custodian/location, provenance label)."""
    data_dir = _data_dir()
    input_dir = data_dir / "input"
    if db_path is None:
        db_path = str(data_dir / "dsar.sqlite")
    if manifest_path is None:
        manifests = sorted((data_dir / "output").glob("pst_manifest_*.sha256"))
        manifest_path = str(manifests[-1]) if manifests else None
    if out_path is None:
        out_path = str(data_dir / "output" / "provenance_catalogue.csv")
    cfg = load_config(config_path)
    manifest = provenance.load_manifest(Path(manifest_path)) if manifest_path else None
    click.echo(f"Manifest: {manifest_path or '(none)'}")

    with db.connect(db_path) as conn:
        db.init_schema(conn)
        counters = provenance.build(conn, input_dir, cfg.provenance, manifest)
        conn.commit()
        n = provenance.export_catalogue(conn, Path(out_path))

    click.echo("Provenance complete:")
    for k, v in counters.items():
        click.echo(f"  {k}: {v}")
    click.echo(f"Wrote {n} rows to {out_path}")
    if counters["manifest_mismatch"]:
        raise click.ClickException(f"{counters['manifest_mismatch']} file(s) do not match the manifest")


@main.command(name="report")
@click.option("--db-path", default=None, help="SQLite path. Defaults to $DSAR_DATA_DIR/dsar.sqlite.")
@click.option("--out", "out_path", default=None, help="CSV output path. Defaults to $DSAR_DATA_DIR/output/results.csv.")
def report_cmd(db_path: str | None, out_path: str | None) -> None:
    """Export a reviewer-friendly CSV of alias-matched chunks sorted by score desc."""
    data_dir = _data_dir()
    if db_path is None:
        db_path = str(data_dir / "dsar.sqlite")
    if out_path is None:
        out_dir = data_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "results.csv")
    with db.connect(db_path) as conn:
        db.init_schema(conn)
        n = report.export_csv(conn, out_path)
    click.echo(f"Wrote {n} rows to {out_path}")


if __name__ == "__main__":
    main()
