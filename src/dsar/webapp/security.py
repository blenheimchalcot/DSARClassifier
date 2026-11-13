"""
Request guards for the local web app.

- Only Host 127.0.0.1 / localhost is served (blocks DNS-rebinding pages).
- The first visit must carry the one-off token; it is swapped for a session
  cookie (SameSite=Strict) and the token is removed from the address bar.
- Every page except sign-in needs a reviewer name.
- Every state-changing request needs the session's CSRF value, in the
  X-CSRF / X-Token header or a `csrf` form field.
- Responses forbid loading anything from other origins, so an HTML email
  shown in the viewer cannot fetch remote images (tracking pixels) or scripts.
"""
import secrets
from urllib.parse import urlencode

from flask import abort, redirect, render_template, request, session, url_for

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
_OPEN_ENDPOINTS = {"main.signin", "main.denied", "static"}
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def install(app) -> None:
    settings = app.config["DSAR"]

    @app.before_request
    def _guard():
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in _ALLOWED_HOSTS:
            abort(400)
        endpoint = request.endpoint or ""
        if endpoint == "static" or endpoint.endswith(".static"):
            return None
        token = request.args.get("token")
        if token and secrets.compare_digest(token, settings.token):
            session.clear()
            session["ok"] = True
            session["csrf"] = secrets.token_urlsafe(24)
            args = {k: v for k, v in request.args.items() if k != "token"}
            return redirect(request.path + ("?" + urlencode(args) if args else ""))
        if not session.get("ok"):
            if endpoint == "main.denied":
                return None
            return render_template("denied.html"), 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = (request.headers.get("X-CSRF") or request.headers.get("X-Token")
                    or request.form.get("csrf") or "")
            if not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(403)
        if not session.get("name") and endpoint not in _OPEN_ENDPOINTS:
            return redirect(url_for("main.signin", next=request.full_path))
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp
