"""
Local web app for a DSAR case: `dsar serve`.

One Flask server on 127.0.0.1 hosts the case pages and every toolbox tool
(docs/TOOLBOX.md, docs/REVIEW_APP_PLAN.md). It runs in one of two modes:

  case-bound    started with a case (DSAR_DATA_DIR). Mailboxes are opened only
                from the input manifest after a hash check, exports are saved
                read-only inside the case with their SHA-256, and actions are
                written to the case audit log under the signed-in reviewer.
  free-standing started with --free. Any mailbox path can be opened and exports
                download to the browser. Nothing is logged; a banner says so.

Access needs the one-off address printed by `dsar serve` (it carries a token);
after that, a session cookie for this server only. Reviewers sign in with their
name, which is recorded with what they do. It is not an identity system.
"""
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask

from dsar import __version__


@dataclass
class Settings:
    data_dir: Path | None = None            # None = free-standing
    config_path: Path | None = None
    markup_folders: list[Path] = field(default_factory=list)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    subject_name: str = ""
    case_ref: str = ""
    cases_root: Path | None = None          # where the case picker looks (webapp.cases)

    @property
    def case_bound(self) -> bool:
        return self.data_dir is not None

    @property
    def output_dir(self) -> Path | None:
        return self.data_dir / "output" if self.data_dir else None

    @classmethod
    def for_case(cls, data_dir: Path, config_path: Path | None = None, **kw) -> "Settings":
        data_dir = Path(data_dir).expanduser()
        if config_path is None and (data_dir / "config.yaml").is_file():
            config_path = data_dir / "config.yaml"
        subject = ""
        if config_path and Path(config_path).is_file():
            try:
                from dsar.config import load_config
                subject = load_config(config_path).subject.name
            except Exception:
                subject = ""
        case_ref = data_dir.parent.name if data_dir.name == "data" else data_dir.name
        kw.setdefault("cases_root", data_dir.parent.parent if data_dir.name == "data" else data_dir.parent)
        return cls(data_dir=data_dir, config_path=Path(config_path) if config_path else None,
                   subject_name=subject, case_ref=case_ref, **kw)


def create_app(settings: Settings) -> Flask:
    from dsar import toolbox
    from dsar.toolbox.mailbox.store import MailboxStore
    from dsar.webapp import review, security, views

    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(
        DSAR=settings,
        SESSION_COOKIE_NAME="dsar_" + secrets.token_hex(4),   # unique per server: cookies ignore the port
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        JSON_SORT_KEYS=False,
    )
    app.extensions["dsar_mailboxes"] = MailboxStore(settings.data_dir)
    security.install(app)
    app.register_blueprint(views.bp)
    app.register_blueprint(review.bp)
    for tool in toolbox.TOOLS:
        app.register_blueprint(tool.blueprint(), url_prefix=f"/toolbox/{tool.key}")

    @app.context_processor
    def _globals():
        from flask import session
        return {"settings": settings, "tools": toolbox.TOOLS, "version": __version__,
                "reviewer": session.get("name", ""), "csrf": session.get("csrf", "")}

    return app


def make_server(settings: Settings, port: int = 0):
    """A threaded server on 127.0.0.1; returns (server, url with the one-off token)."""
    from werkzeug.serving import WSGIRequestHandler
    from werkzeug.serving import make_server as _make

    class _Quiet(WSGIRequestHandler):
        # Request lines carry search terms, folder and file names: keep them out of the terminal.
        def log_request(self, *args, **kwargs):
            pass

    app = create_app(settings)
    server = _make("127.0.0.1", port, app, threaded=True, request_handler=_Quiet)
    app.config["DSAR_PORT"] = server.server_port
    return server, f"http://127.0.0.1:{server.server_port}/?token={settings.token}"
