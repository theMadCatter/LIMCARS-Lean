"""HTTP server: stdlib ThreadingHTTPServer + a tiny router.

Routes
  GET  /                      dashboard (web/dashboard.html)
  GET  /api/health            {ok, version, uptime}
  GET  /api/snapshot          engines + hw latest + rings (cold-start paint)
  GET  /api/history?engine=&seconds=   sqlite downsample history
  GET  /api/events            SSE: topics engine:*, hw, requests, events
  POST /api/services/<id>/start|stop   whitelisted systemd/docker actions
  GET  /static/*              assets

SSE framing: `id:` per event so a dropped tab resumes with Last-Event-ID.
`?topics=engine:flashnext,hw` filters subscriptions client-side server-side.
"""
from __future__ import annotations

import gzip
import json
import hmac
import logging
import mimetypes
import posixpath
import re
import threading
import time
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .sse import Broker

VERSION = "1.1.0"
_START_T = time.time()

# Session cookie carried on every browser request. HttpOnly + SameSite=Strict
# are set on the Set-Cookie header below; "Strict" ensures a cross-site POST
# (DNS-rebinding, a malicious link) cannot ride a logged-in tab's cookie
# into a mutating endpoint. Secure flag is only set when the request itself
# came over https (we can't blindly turn it on in a local-only install).
SESSION_COOKIE = "limcars_session"

# Static assets served to anonymous callers (so the login page itself works
# before any session exists). Everything NOT in this list requires auth when
# the gate is enabled.
_PUBLIC_PATHS = {
    "/favicon.svg", "/favicon.png", "/favicon.ico",
    "/login", "/login/", "/login.html",
    "/api/health",             # unauth'd liveness probe (no sensitive data)
    "/api/auth/status",        # the UI needs to know whether to prompt
    "/api/auth/login",         # the login POST itself
}
_PUBLIC_PREFIXES = ("/css/", "/js/", "/images/")
# Routes that execute commands or spawn processes. Gated behind cfg.allow_exec
# AND cfg.auth; every single one of these used to be reachable without the
# admin password (recipes start/stop, services start/stop, engine kill, probe).
_EXEC_WRITE_RE = (
    re.compile(r"^/api/recipes/?$"),                                 # add
    re.compile(r"^/api/recipes/[\w-]+/(update|delete|start|stop)$"), # mutate+exec
    re.compile(r"^/api/services/[\w-]+/(start|stop)$"),              # exec
    re.compile(r"^/api/engines-live/\d+/stop$"),                     # kill
    re.compile(r"^/api/probe/?$"),                                   # run completion
)

# Panel settings — same spirit/keys as old LIMCARS dash_settings.json:
# server-side persisted, shared across all browsers.
SETTINGS_DEFAULTS = {
    "bubbles_main": True,       # the ordered main bubble strip
    "bubbles_live": True,       # (legacy keys kept harmless)
    "bubbles_latency": True,
    "bubbles_spec": True,
    "bubbles_savings": True,    # cost-savings bubbles
    "bubbles_potential": True,  # 'lost potential' wide bubble under savings
    "charts_infer": True,       # KV / prefill / decode chart row
    "charts_prom": True,        # TTFT / e2e / hit% chart row
    "chart_per_client": True,    # per-client context chart
    "chart_per_model": True,     # stats-by-model table
    "chart_savings_daily": True,  # daily money-saved chart (real usage)
    "requests_feed": True,      # recent completed requests table
    "refresh_s": 1.0,           # base UI refresh interval (0.5-30 s)
    "stats_scope": "all",       # all | active engine
    # theme + palette are NOT server settings: per-browser cookie (limcars_ui)
}


# probe result cache: key -> (monotonic_time, result, ttl_seconds)
_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: dict[str, tuple[float, dict, float]] = {}


class _Settings:
    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()
        self.data = dict(SETTINGS_DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, json.JSONDecodeError):
            pass

    def all(self) -> dict:
        with self.lock:
            return dict(self.data)

    def update(self, patch: dict) -> None:
        with self.lock:
            self.data.update(patch)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.path)


class Limcars:
    """Dependency container shared with handlers."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.broker: Broker = Broker()
        from .store import Store
        self.store = Store(cfg["db_path"], float(cfg.get("persist_seconds", 3600)))
        from .sampler import EngineManager
        self.engines = EngineManager(cfg, self.broker, self.store)
        from .hw import HwSampler
        self.hw = HwSampler(cfg, self.broker, self.store)
        self.hw.start()
        from .nodes import start_nodes
        self.nodes = start_nodes(cfg, self.broker)   # agentless cluster nodes (SSH)
        from .services import Services
        self.services = Services(cfg)
        from .recipes import Recipes
        # `recipes_dir` lets tests (and alternative deployments) target a
        # scratch directory instead of the shared data/. When unset we keep
        # the historical path so no existing install moves.
        self.recipes = Recipes(cfg.get("recipes_dir") or str(config.DATA))
        from .live_engines import LiveEngines
        self.live_engines = LiveEngines()
        self._savhist_cache: dict[int, tuple] = {}   # days -> (ts, payload)
        self._lostpot_cache: dict[str, tuple] = {}   # 'all' -> (ts, payload)
        from .clients import ClientSampler
        self.clients = ClientSampler(self.broker)
        self.clients.start()
        from .status_tiles import StatusTileSampler
        # Optional status tiles for ancillary services (ComfyUI, Kokoro
        # TTS, LiteLLM proxy, …). Thread stays idle when
        # cfg["status_tiles"] is empty, so this is free when unused.
        self.status_tiles = StatusTileSampler(cfg)
        self.status_tiles.start()
        self.settings = _Settings(cfg.get("settings_path", "data/settings.json"))
        from .auth import AuthGate
        self.auth = AuthGate(cfg.get("auth_path") or str(config.DATA / "auth.json"),
                             session_ttl=cfg.get("session_ttl_s"))

    def settings_all(self) -> dict:
        return self.settings.all()

    def settings_update(self, patch: dict) -> None:
        self.settings.update(patch)


_TOPIC_RE = re.compile(r"[A-Za-z0-9:_-]{1,64}")
# static text assets are gzipped once per (mtime, size) and served to any
# client that accepts it; the bytes the browser decodes are unchanged
_GZIP_MIN = 1024
_GZIP_TYPES = ("text/", "application/javascript", "application/json", "image/svg+xml")
_gzip_cache: dict[str, tuple[tuple[int, int], bytes]] = {}


def _gzipped(f: Path, data: bytes) -> bytes:
    st = f.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _gzip_cache.get(str(f))
    if hit and hit[0] == key:
        return hit[1]
    gz = gzip.compress(data, 9, mtime=0)
    _gzip_cache[str(f)] = (key, gz)
    return gz


def _sse_format(topic: str, sid: int, data: str) -> bytes:
    return f"id: {topic}:{sid}\nevent: {topic}\ndata: {data}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    # audit: thread-per-connection with no socket timeout let slow-loris
    # sockets pin threads forever. 30 s per read; SSE disables it (write-only).
    timeout = 30
    server_version = f"limcars-next/{VERSION}"
    app: Limcars  # set on the class by serve()

    # ---------------- plumbing ----------------

    def log_message(self, format: str, *args) -> None:  # quieter default log
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _write(self, body: bytes) -> None:
        # HEAD must answer like GET but with no body. BaseHTTPRequestHandler
        # ships no do_HEAD, so HEAD used to fall through and get the HTML page
        # — which is how 'curl -I /js/dashboard.js' called a script text/html.
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    # ---------- auth helpers ----------
    #
    # Three independent credentials can satisfy the "logged in" check, in
    # this precedence order:
    #   1. a bearer token (`X-Limcars-Token` / `Authorization: Bearer …`)
    #      matching cfg["auth_token"] — for scripts/curl.
    #   2. a valid session cookie minted by /api/auth/login — for browsers.
    #   3. the request originated on localhost AND no auth is required
    #      (loopback bind with neither a token nor a password set).
    #
    # Previously the server fell through to "True" whenever the token was
    # unset, which left EVERY read AND write endpoint unauthenticated on
    # the default LAN bind. This method now returns True ONLY when one of
    # the three conditions above explicitly holds.

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _is_local(self) -> bool:
        return self._client_ip() in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _cookie(self, name: str) -> str:
        """Lookup one cookie value from the request. Keeps parsing cheap and
        tolerant: a malformed header reads as absent, never as a crash."""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _bearer_token(self) -> str:
        """Return the bearer token presented on this request, if any. Both
        the custom X-Limcars-Token header and the standard Authorization:
        Bearer form are accepted (symmetric with how /api/auth/login takes
        them). A duplicated header is treated as absent — never trust a
        request-smuggled auth header split."""
        hdrs = self.headers.get_all("X-Limcars-Token") or []
        if len(hdrs) > 1:
            return ""
        if hdrs:
            return hdrs[0]
        a = self.headers.get("Authorization", "")
        if a.startswith("Bearer "):
            return a[len("Bearer "):]
        return ""

    def _auth_required(self) -> bool:
        """True when every route must present credentials. Loopback-only
        installs with no password and no bearer token stay zero-config (so
        a solo-dev `python3 -m app` still just works), but ANY exposure
        beyond loopback or ANY credential being set switches the gate on."""
        cfg = self.app.cfg
        if cfg.get("auth_token") or self.app.auth.enabled:
            return True
        if not config.is_loopback(cfg.get("host", "")):
            return True
        return False

    def _bearer_matches(self) -> bool:
        tok = self.app.cfg.get("auth_token")
        if not tok:
            return False
        return hmac.compare_digest(self._bearer_token() or "", tok)

    def _session_matches(self) -> bool:
        return self.app.auth.check_session(self._cookie(SESSION_COOKIE))

    def _authorized(self) -> bool:
        """Request-level auth check used by both page and API dispatchers."""
        if not self._auth_required():
            return True
        if self._bearer_matches():
            return True
        if self._session_matches():
            return True
        # Loopback with auth configured still has to log in — the old code
        # exempted localhost here, which meant a reverse proxy / container
        # bridge that landed on 127.0.0.1 bypassed the password entirely.
        return False

    def _pw_from_headers(self, default: str = "") -> str:
        """Read the shared password from headers. A duplicated X-Limcars-Pw
        header is never legitimate and is treated as absent (audit: harden
        against request-smuggling style header splits)."""
        hdrs = self.headers.get_all("X-Limcars-Pw") or []
        if len(hdrs) > 1:
            return default
        return (hdrs[0] if hdrs else "") or default

    def _require_exec(self, path: str) -> bool:
        """Gate every command-executing endpoint.

        Two independent checks, both have to pass:
          * `allow_exec` must be true in config. A fresh install ships this
            OFF so a mis-set or absent password can never spawn a process.
          * when a password is set, the request must present it (same
            lockout ladder as the old `_require_write`). An admin-only
            session cookie is NOT enough on its own — these actions SPAWN
            PROCESSES and we want a fresh password proof for each.

        Localhost is NOT exempt anymore: see the file-level comment in
        auth.py for the reasoning. Opt out explicitly with the
        ``allow_exec_localhost_exempt`` config flag if a legacy deployment
        needs the old behaviour.
        """
        if not self.app.cfg.get("allow_exec"):
            self._json({"ok": False, "error": "command execution disabled",
                        "hint": "set 'allow_exec': true in data/config.json "
                                "or export LIMCARS_ALLOW_EXEC=1 to turn on"},
                       code=403)
            return False
        if not self.app.auth.enabled:
            # No password set at all: refuse unless a bearer token is also
            # configured. The bearer proves this request came from someone
            # trusted even without a password (e.g. a scripted agent).
            if self._bearer_matches():
                return True
            # Localhost-with-no-password opt-out for legacy single-user boxes.
            if self.app.cfg.get("allow_exec_localhost_exempt") and self._is_local():
                return True
            self._json({"ok": False, "error": "set an admin password first",
                        "hint": "open the dashboard, use the Security panel "
                                "to set a password, then retry"},
                       code=403)
            return False
        ip = self._client_ip()
        pw = self._pw_from_headers()
        ok, rem = self.app.auth.attempt(ip, pw)
        if ok:
            return True
        if rem > 0:
            self._json({"ok": False, "error": "locked",
                        "retry_after": round(rem, 1)}, code=423)
        else:
            self._json({"ok": False, "error": "wrong password"}, code=401)
        return False

    # ---------------- dispatch ----------------

    def do_HEAD(self) -> None:  # noqa: N802
        # monitoring, link checkers and curl -I all ask; answer like GET
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            # Public (no-auth) surface: the login page, its CSS/JS, the
            # health probe and the auth status endpoint. Everything else
            # requires one of {bearer, session cookie, or an auth-off
            # loopback install} once auth is required at all.
            if self._auth_required() and not self._is_public_get(path):
                if not self._authorized():
                    # Pages redirect to /login, APIs get a JSON 401 so the
                    # JS clients can surface the right message.
                    if path.startswith("/api/") or path.startswith("/subagent-watch"):
                        return self._err(401, "unauthorized")
                    return self._redirect_to_login()
            if path.startswith("/api/"):
                return self._api_get(path, qs)
            if path == "/login" or path == "/login/" or path == "/login.html":
                return self._static("/login.html")
            if path.startswith("/subagent-watch"):
                return self._swatch(path, qs)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            # audit: never echo exception text to remote clients (internals
            # leak); the full traceback goes to the server log only
            logging.getLogger("limcars").exception("GET %s failed",
                                                  self.path.split("?", 1)[0])
            try:
                self._err(500, "internal error")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        # audit: an unhandled exception in a POST branch used to escape to
        # BaseHTTPRequestHandler (dropped connection, no response) — every
        # client now gets a clean 500 and the traceback goes to the log
        # audit: DNS-rebinding / cross-site forms would arrive with peer
        # 127.0.0.1 and ride the localhost exemption straight past the recipe
        # gate. A browser always sends Origin on cross-site POSTs — reject
        # any whose host is not the host the client asked for. (CLI/agents
        # send no Origin and are unaffected.)
        org = self.headers.get("Origin")
        if org:
            try:
                oh = urllib.parse.urlparse(org).hostname or ""
            except ValueError:
                oh = "?"
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if oh and host and oh not in ("localhost", "127.0.0.1", "::1") \
                    and oh != host:
                return self._err(403, "cross-origin write refused")
        try:
            self._dispatch_post()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            logging.getLogger("limcars").exception("POST %s failed",
                                                  self.path.split("?", 1)[0])
            try:
                self._err(500, "internal error")
            except Exception:  # noqa: BLE001
                pass

    def _dispatch_post(self) -> None:
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._err(404, "not found")
        # /api/auth/login is the only POST that MUST stay reachable before
        # any session exists — otherwise a user with auth enabled could not
        # log in from the browser. /api/auth/status is a public GET but has
        # no POST shape, so we don't need to list it here.
        if u.path not in ("/api/auth/login",):
            if self._auth_required() and not self._authorized():
                return self._err(401, "unauthorized")
        # Login handler: trade a password or the configured bearer token for
        # a session cookie. Uses AuthGate.attempt() so the password path
        # shares the lockout ladder with recipe writes.
        if re.fullmatch(r"/api/auth/login/?", u.path):
            return self._login()
        if re.fullmatch(r"/api/auth/logout/?", u.path):
            tok = self._cookie(SESSION_COOKIE)
            self.app.auth.revoke_session(tok)
            return self._json({"ok": True}, code=200)
        if re.fullmatch(r"/api/auth/set/?", u.path):
            body = self._body_json()
            if body is None:
                return
            new = (body.get("new") or "").strip()
            cur = body.get("current")
            gate = self.app.auth
            if gate.enabled:
                # Changing an existing password proves knowledge of it first
                # (atomic lockout ladder via attempt()). The old "localhost
                # exempt" shortcut here is deliberately gone: a reverse proxy
                # that terminates on 127.0.0.1 is not implicitly trusted.
                ok, rem = gate.attempt(self._client_ip(), cur or "")
                if rem > 0:
                    return self._json({"ok": False, "error": "locked",
                                       "retry_after": round(rem, 1)}, code=423)
                if not ok:
                    return self._json({"ok": False, "error": "wrong password"},
                                      code=401)
            return self._json({"ok": gate.set_password(new or None, cur)})
        # EVERY command-executing route sits behind _require_exec: the
        # `allow_exec` config flag AND (when a password is set) a fresh
        # password submission. The exec-write regexes are declared next to
        # the public-path allowlist at the top of the file.
        if any(rx.fullmatch(u.path) for rx in _EXEC_WRITE_RE):
            if not self._require_exec(u.path):
                return
        m = re.fullmatch(r"/api/services/([\w-]+)/(start|stop)", u.path)
        if m:
            out = self.app.services.action(m.group(1), m.group(2))
            return self._json(out)
        m = re.fullmatch(r"/api/recipes/?", u.path)
        if m:
            body = self._body_json()
            if body is None:
                return
            return self._json(self.app.recipes.add(body.get("name"), body.get("cmd")))
        m = re.fullmatch(r"/api/recipes/([\w-]+)/(update|delete|start|stop)", u.path)
        if m:
            rid, act = m.group(1), m.group(2)
            if act == "update":
                body = self._body_json()
                if body is None:
                    return
                return self._json(self.app.recipes.update(
                    rid, name=body.get("name"), cmd=body.get("cmd")))
            if act == "delete":
                return self._json(self.app.recipes.delete(rid))
            return self._json(getattr(self.app.recipes, act)(rid))
        m = re.fullmatch(r"/api/engines-live/(\d+)/stop", u.path)
        if m:
            return self._json(self.app.live_engines.stop(int(m.group(1))))
        m = re.fullmatch(r"/api/probe/?", u.path)
        if m:
            from . import probe as probe_mod
            # Pick the engine the user actually selected (?engine=<id>);
            # earlier builds hardcoded "the first configured engine" and so a
            # probe button for a non-default engine fired at the wrong box.
            engine_id = None
            try:
                q = urllib.parse.parse_qs(u.query)
                engine_id = (q.get("engine") or [None])[0]
            except ValueError:
                engine_id = None
            smp = None
            if engine_id:
                smp = self.app.engines.get(engine_id)
            if smp is None:
                smp = next(iter(self.app.engines.all()), None)
            if not smp:
                return self._err(400, "no engine configured")
            # A probe is a real 1300-word generation on the engine. Cache the
            # result (success 5 min / failure 30 s) so refresh loops and
            # multiple tabs can't hammer the box (audit: no budget existed).
            key = f"{smp.url}|{smp.model}"
            now = time.time()
            with _PROBE_LOCK:
                hit = _PROBE_CACHE.get(key)
                if hit and now - hit[0] < hit[2]:
                    return self._json(hit[1])
            try:
                # Pass the engine config so an API-key-protected engine
                # answers the probe instead of 401'ing.
                res = probe_mod.probe(smp.url, smp.model, eng=smp.eng)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)[:300]}
            with _PROBE_LOCK:
                _PROBE_CACHE[key] = (now, res, 300 if res.get("ok") else 30)
            return self._json(res)
        m = re.fullmatch(r"/api/config/?", u.path)
        if m:
            return self._config_patch()
        m = re.fullmatch(r"/api/settings/?", u.path)
        if m:
            return self._settings_patch()
        return self._err(404, "not found")

    def _login(self) -> None:
        """Trade a password (or the configured bearer token) for a session
        cookie. Rate-limited through AuthGate.attempt() so a brute-forcer
        hits the same ladder as the recipe-write gate (3 bad -> 60 s …).

        Accepting the bearer token here means `curl -d token=…
        /api/auth/login` is enough to prime a cookie jar — useful for
        scripted agents that then want to browse the dashboard without
        attaching the token on every request.
        """
        body = self._body_json()
        if body is None:
            return
        pw = (body.get("password") or body.get("pw") or "").strip()
        tok = (body.get("token") or "").strip()
        gate = self.app.auth
        ip = self._client_ip()
        cfg_tok = self.app.cfg.get("auth_token")
        if tok and cfg_tok and hmac.compare_digest(tok, cfg_tok):
            # Bearer login — clear any failure counter the IP accumulated,
            # so a user who typo'd their password but then pasted the token
            # isn't still locked out.
            gate.success(ip)
            sess = gate.issue_session(ip)
            return self._send_session(sess)
        if gate.enabled:
            ok, rem = gate.attempt(ip, pw)
            if ok:
                sess = gate.issue_session(ip)
                return self._send_session(sess)
            if rem > 0:
                return self._json({"ok": False, "error": "locked",
                                   "retry_after": round(rem, 1)}, code=423)
            return self._json({"ok": False, "error": "wrong password"}, code=401)
        if cfg_tok:
            # Password not set but a bearer token IS configured and the
            # caller didn't send it in body.token — reject rather than
            # silently log them in. (Bearer header auth still works for
            # scripts; this is just the cookie-mint path.)
            return self._json({"ok": False, "error": "token required"},
                              code=401)
        # No auth configured and no auth required: just issue a cookie so
        # the UI's "logout" button has something to clear.
        sess = gate.issue_session(ip)
        self._send_session(sess)

    def _send_session(self, token: str) -> None:
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Cookie attributes:
        #   HttpOnly   — JS can't read the token (XSS can't exfiltrate it)
        #   SameSite=Strict — browsers refuse to attach it to cross-site
        #                     requests, which is our CSRF defense in depth
        #                     on top of the existing Origin header check
        #   Path=/     — scope covers both pages and /api/*
        attrs = (f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; "
                 f"Path=/; Max-Age={int(self.app.auth.session_ttl)}")
        self.send_header("Set-Cookie", attrs)
        self.end_headers()
        self._write(body)

    def _redirect_to_login(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _is_public_get(self, path: str) -> bool:
        if path in _PUBLIC_PATHS:
            return True
        for p in _PUBLIC_PREFIXES:
            if path.startswith(p):
                return True
        return False

    # NOTE: `allow_exec` is deliberately NOT in this set. Flipping the master
    # exec switch has to be an EXPLICIT edit of data/config.json (or the env
    # var at boot). A compromised UI session must not be able to turn on
    # command execution over the API.
    _CONFIG_ALLOWED = {"pricing", "window_s", "live_hz", "hw_hz"}
    SETTING_DEFAULTS = SETTINGS_DEFAULTS

    def _body_json(self) -> dict | None:
        """Read+parse a JSON POST body; sends 400 and returns None on failure."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            # audit: unbounded body read = one-POST OOM of the whole monitor
            if n < 0 or n > 1_000_000:
                self._err(413, "body too large")
                return None
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._err(400, "bad json")
            return None
        if not isinstance(body, dict):
            self._err(400, "json object expected")
            return None
        return body

    def _config_patch(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1_000_000:  # audit: bypassed the _body_json cap
                return self._err(400, "bad body size")
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err(400, "bad json")
        if not isinstance(body, dict) or not (set(body) <= self._CONFIG_ALLOWED):
            return self._err(400, f"only {sorted(self._CONFIG_ALLOWED)} are editable")
        # audit: keys were whitelisted, VALUES were not — pricing:"x" poisoned
        # every $ endpoint and tripped the sampler's fake engine_down path;
        # window_s:"x" persisted and crashed boot on the next restart.
        if "pricing" in body and body["pricing"] is not None:
            pv = body["pricing"]
            labels = ("display_name", "provider")
            if not isinstance(pv, dict) or any(
                    isinstance(v, bool) or not isinstance(v, (int, float))
                    for k, v in pv.items() if k not in labels) or any(
                    not isinstance(pv[k], str) for k in labels if k in pv):
                return self._err(400, "pricing must be a dict of numbers "
                                      "(plus optional display_name/provider strings)")
        for k in ("window_s", "live_hz", "hw_hz"):
            if k in body:
                try:
                    v = float(body[k])
                except (TypeError, ValueError):
                    return self._err(400, f"{k} must be a number")
                lo, hi = (1.0, 86400.0) if k == "window_s" else (0.05, 20.0)
                if not lo <= v <= hi:
                    return self._err(400, f"{k} must be within {lo}..{hi}")
        app = self.app
        if "live_hz" in body:
            body["live_hz"] = min(4.0, max(1.0, float(body["live_hz"])))
        app.cfg.update(body)
        # persist to data/config.json (merge)
        from . import config as cfg_mod
        f = cfg_mod.DATA / "config.json"
        disk = {}
        if f.exists():
            try:
                disk = json.loads(f.read_text())
            except ValueError:
                disk = {}
        disk.update({k: v for k, v in body.items() if k in self._CONFIG_ALLOWED})
        f.write_text(json.dumps(disk, indent=2))
        self._json({"ok": True, "config": {k: app.cfg.get(k) for k in body}})

    # ---------------- API ----------------

    def _api_get(self, path: str, qs: dict) -> None:
        app = self.app
        if path == "/api/health":
            return self._json({"ok": True, "version": VERSION,
                               "uptime_s": round(time.time() - _START_T, 1)})
        if path == "/api/auth/status":
            # Reports the password gate, whether a bearer token is set,
            # whether this session is already authenticated, whether command
            # execution is enabled (so the UI can grey out Start/Stop
            # buttons), and the per-IP lockout remaining. All of this is
            # safe to expose anonymously — none of it reveals a credential.
            return self._json({
                "enabled": app.auth.enabled,
                "token_set": bool(app.cfg.get("auth_token")),
                "authed": self._authorized(),
                "auth_required": self._auth_required(),
                "allow_exec": bool(app.cfg.get("allow_exec")),
                "locked_for_you": round(app.auth.locked_for(self._client_ip()), 1),
            })
        if path == "/api/snapshot":
            secs = (qs.get("seconds") or [None])[0]
            try:
                secs = float(secs) if secs else None
            except (TypeError, ValueError):
                secs = None
            if secs is not None:
                # audit: seconds=1e18 scanned the whole samples table under
                # the store lock (sampler stall + OOM per request)
                import math as _m
                if not _m.isfinite(secs) or secs <= 0:
                    secs = None
                else:
                    secs = min(secs, float(self.app.cfg.get(
                        "persist_seconds", 3600)) + 300)
            eng_snap = app.engines.snapshots(secs)
            _sel = (qs.get("engine") or [None])[0]
            _model = (eng_snap.get(_sel) or next(iter(eng_snap.values()), {})).get("model")
            from . import pricing as _pr
            return self._json({
                "ok": True, "version": VERSION,
                "ts": time.time(), "uptime_s": round(time.time() - _START_T, 1),
                "pricing": self.app.cfg.get("pricing") or _pr.resolve(_model),
                "engines": eng_snap,
                "hw": app.hw.snapshot(secs),
                "nodes": {n.id: n.snapshot(secs) for n in app.nodes},
                "services": app.services.status_all(),
            })
        if path == "/api/history":
            eng = (qs.get("engine") or [None])[0]
            secs = float((qs.get("seconds") or ["900"])[0])
            if eng:
                return self._json({"ok": True, "rows": app.store.history("engine_samples", eng, secs)})
            return self._json({"ok": True, "rows": app.store.history("hw_samples", None, secs)})
        if path == "/api/events":
            return self._sse(qs)
        if path == "/api/savings-history":
            # daily money-saved chart data (real persisted usage, per model)
            from . import counters as counters_mod
            try:
                days = int((qs.get("days") or ["30"])[0])
            except ValueError:
                return self._err(400, "bad days")
            if days not in (7, 30, 92, 365):
                days = 30
            # 60 s TTL cache: the chart re-renders every 60 s per open tab;
            # day buckets move at most once per minute anyway (audit F4)
            cache = getattr(app, "_savhist_cache", None)
            if cache is None:
                cache = app._savhist_cache = {}
            hit = cache.get(days)
            now = time.time()
            if hit and now - hit[0] < 60.0:
                return self._json(hit[1])
            engines = [{"id": s.id, "model": s.model} for s in app.engines.all()]
            payload = {"ok": True, **counters_mod.daily_savings(
                app.store, engines, app.cfg.get("pricing"), days)}
            cache[days] = (now, payload)
            return self._json(payload)
        if path == "/api/lost-potential":
            # 'lost potential' bubble: peak day/month/year vs recorded averages
            from . import counters as counters_mod
            cache = getattr(app, "_lostpot_cache", None)
            if cache is None:
                cache = app._lostpot_cache = {}
            hit = cache.get("all")
            now = time.time()
            if hit and now - hit[0] < 60.0:
                return self._json(hit[1])
            engines = [{"id": s.id, "model": s.model, "alive": bool(s.alive)}
                       for s in app.engines.all()]
            # machine occupancy: only set this in config to PIN the hours/day;
            # left unset, the dash uses the hours a day it actually measured
            duty = app.cfg.get("duty_hours_per_day")
            payload = counters_mod.lost_potential(
                app.store, engines, app.cfg.get("pricing"), duty_hours=duty)
            cache["all"] = (now, payload)
            return self._json(payload)
        m = re.fullmatch(r"/api/counters/?", path)
        if m:
            from . import counters as counters_mod
            smp = next(iter(self.app.engines.all()), None)
            if not smp or not self.app.store:
                return self._json({"empty": True})
            win = (qs.get("window") or ["all"])[0]
            return self._json(counters_mod.report(
                self.app.store, smp.id, win, smp.model,
                override=self.app.cfg.get("pricing")))
        m = re.fullmatch(r"/api/engine-info/?", path)
        if m:
            from . import engine_info
            engine_id = (qs.get("engine") or [None])[0]
            smp = (self.app.engines.get(engine_id) if engine_id else None) \
                or next(iter(self.app.engines.all()), None)
            if not smp:
                return self._json({"healthy": False, "error": "no engine"})
            # Pass the engine config so per-engine auth headers (api_key,
            # Authorization overrides) are attached to the /metrics,
            # /v1/models and /version calls.
            return self._json(engine_info.info(smp.url, smp.eng))
        m = re.fullmatch(r"/api/settings/?", path)
        if m:
            return self._json(self.app.settings_all())
        m = re.fullmatch(r"/api/session-tokens/?", path)
        if m:
            from . import session_tokens
            smp = next(iter(self.app.engines.all()), None)
            want = (qs.get("model") or [""])[0] or (smp.model if smp else None)
            return self._json(session_tokens.today_sessions(want))
        m = re.fullmatch(r"/api/clients/?", path)
        if m:
            win = min(86400.0, max(60.0, float((qs.get("window_s") or ["1800"])[0])))
            return self._json(self.app.clients.window(win))
        m = re.fullmatch(r"/api/recipes/?", path)
        if m:
            return self._json({"recipes": self.app.recipes.list()})
        m = re.fullmatch(r"/api/engines-live/?", path)
        if m:
            return self._json({"engines": self.app.live_engines.scan(
                force=(qs.get("refresh") == ["1"])), "at": time.time()})
        m = re.fullmatch(r"/api/status-tiles/?", path)
        if m:
            # Status tiles are read-only observers of ancillary services
            # (ComfyUI /queue, generic HTTP health, etc). The sampler is
            # polling in the background — this endpoint just hands back
            # the most recent snapshot.
            return self._json({"tiles": self.app.status_tiles.snapshot(),
                               "at": time.time()})
        m = re.fullmatch(r"/api/litellm/spend/?", path)
        if m:
            # LiteLLM proxy spend summary. Reads from any engine whose
            # kind is 'litellm' (or an explicit ?engine=<id>), uses the
            # master key configured on that engine entry (api_key /
            # api_key_env). Enterprise metric coverage is documented in
            # dialects.LITELLM — this endpoint is the OSS alternative
            # for seeing per-model spend and token counts.
            import os as _os
            from . import litellm_proxy as _llm
            engine_id = (qs.get("engine") or [None])[0]
            smp = (self.app.engines.get(engine_id) if engine_id else None)
            if smp is None:
                for s in self.app.engines.all():
                    # auto-pick the first litellm engine, same spirit as
                    # the other "first configured" fallbacks
                    if (s.kind or s.eng.get("kind")) == "litellm":
                        smp = s
                        break
            if smp is None:
                return self._err(400, "no LiteLLM engine configured")
            key = (smp.eng.get("api_key")
                   or _os.environ.get(smp.eng.get("api_key_env") or "")
                   or self.app.cfg.get("litellm_master_key")
                   or _os.environ.get("LITELLM_MASTER_KEY"))
            if not key:
                return self._err(400,
                    "set api_key / api_key_env on the engine entry, "
                    "or export LITELLM_MASTER_KEY")
            try:
                days = int((qs.get("days") or ["1"])[0])
            except ValueError:
                days = 1
            days = max(1, min(90, days))
            data = _llm.spend_summary(smp.url, key, days=days)
            return self._json({"ok": True, **data})
        return self._err(404, "not found")

    # ---------------- panel settings (old dash_settings.json parity) ----------------

    def _settings_patch(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1_000_000:  # audit: bypassed the _body_json cap
                return self._err(400, "bad body size")
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err(400, "bad json")
        if not isinstance(body, dict):
            return self._err(400, "object required")
        allowed = set(self.SETTING_DEFAULTS)
        if not (set(body) <= allowed):
            return self._err(400, f"only {sorted(allowed)} are editable")
        for k in ("refresh_s",):
            if k in body:
                body[k] = min(30.0, max(0.5, float(body[k])))
        if "stats_scope" in body and body["stats_scope"] not in ("all", "active"):
            return self._err(400, "stats_scope: all|active")
        # audit: settings must stay JSON scalars — nested values would crash
        # the store's sort keys / sparklines with 500s for every reader
        for k, v in body.items():
            if v is not None and not isinstance(v, (str, int, float, bool)):
                return self._err(400, f"{k}: scalar value expected")
        self.app.settings_update(body)
        self._json({"ok": True, "settings": self.app.settings_all()})

    def _sse(self, qs: dict) -> None:
        if getattr(self, "_head_only", False):
            # HEAD on the event stream must not block waiting for events
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.timeout = None  # audit: the event stream only writes
        topics_wanted = (qs.get("topics") or [""])[0].split(",")
        wanted = {t for t in topics_wanted if t}
        # audit: never create broker topics from client input — only names that
        # are legitimately part of this deployment. The old test compared against
        # topics that had ALREADY been published to, so on a fresh start or with
        # no engine connected 'requests' and 'events' did not exist yet, the
        # browser's own topic list was rejected, and the page retried a 400 every
        # 2 s forever (user: "the web ui keeps refreshing itself very often").
        known = set(self.app.broker._topics.keys())
        core = {"hw", "requests", "events"}
        allowed = (known | core | {f"engine:{s.id}" for s in self.app.engines.all()}
                   | {n.topic for n in self.app.nodes})
        bad = [t for t in wanted
               if not _TOPIC_RE.fullmatch(t) or t not in allowed]
        if len(wanted) > max(32, len(allowed)) or bad:   # big clusters: one topic per node
            return self._err(400, "bad topics")
        # Subscribe to what the broker already carries; a valid topic that has
        # simply not published yet is created on subscribe rather than punished.
        # every requested topic passed the allowlist; keep them all (a node that
        # has not published yet must still stream once its first SSH poll lands)
        wanted &= allowed
        last = self.headers.get("Last-Event-ID")  # "topic:seq"
        since: dict[str, int] = {}
        if last:
            t_, _, s_ = last.partition(":")
            try:
                since[t_] = int(s_)
            except ValueError:
                pass
        app = self.app
        subs = []
        try:
            for name in wanted:
                topic = app.broker.topic(name)
                q, backlog = topic.subscribe(since.get(name))
                subs.append((name, topic, q, backlog))
        except RuntimeError as exc:            # MAX_SUBS reached
            for item in subs:
                item[1].unsubscribe(item[2])
            return self._err(503, str(exc))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for name, topic, q, backlog in subs:
            for sid, data in backlog:
                self.wfile.write(_sse_format(name, sid, data))
        self.wfile.flush()
        idle = 0.0
        try:
            while True:
                got = False
                for name, topic, q, _bl in subs:
                    try:
                        while True:
                            sid, data = q.get_nowait()
                            self.wfile.write(_sse_format(name, sid, data))
                            got = True
                    except Exception:  # noqa: BLE001 — queue.Empty
                        pass
                if got:
                    idle = 0.0
                    self.wfile.flush()
                else:
                    time.sleep(0.1)
                    idle += 0.1
                    if idle > 15:  # keepalive comment defeats proxy idle timeouts
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0.0
        finally:
            for _, topic, q, _bl in subs:
                topic.unsubscribe(q)

    # ---------------- static ----------------

    # ---------------- Hermes Traces (/subagent-watch/*) ----------------

    def _swatch(self, path: str, qs: dict) -> None:
        from . import swatch
        m = re.fullmatch(r"/subagent-watch/batches/?", path)
        if m:
            return self._json(swatch.batches())
        m = re.fullmatch(r"/subagent-watch/main-sessions/?", path)
        if m:
            return self._json(swatch.main_sessions())
        m = re.fullmatch(r"/subagent-watch/manifest/([\w.-]+)/?", path)
        if m:
            r = swatch.manifest(m.group(1))
            return self._json(r) if r is not None else self._err(404, "no batch")
        m = re.fullmatch(r"/subagent-watch/main-transcript/([\w.-]+)/([\w.-]+)/?", path)
        if m:
            limit = int((qs.get("limit") or ["200"])[0])
            r = swatch.main_transcript(m.group(1), m.group(2), limit)
            return self._json(r) if r else self._err(404, "no session")
        m = re.fullmatch(r"/subagent-watch/transcript/([\w.-]+)/([\w.-]+)/?", path)
        if m:
            return self._serve_text(swatch.transcript_file(m.group(1), m.group(2)))
        m = re.fullmatch(r"/subagent-watch/summary/([\w.-]+)/?", path)
        if m:
            return self._serve_text(swatch.summary_file(m.group(1)))
        return self._static(path)   # page + assets

    def _serve_text(self, f) -> None:
        """text/plain with HTTP Range (bytes=-N) — the trace UI's tail poll."""
        from . import swatch as _sw
        if f is None:
            return self._err(404, "not found")
        try:
            size = f.stat().st_size
            data = f.read_bytes()
        except OSError:
            return self._err(404, "not found")
        try:
            rng = _sw.parse_range(self.headers.get("Range"), size)
        except Exception:  # noqa: BLE001
            rng = None
        if rng:
            start, end = rng
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/dashboard.html"
        if path in ("/subagent-watch", "/subagent-watch/"):
            path = "/subagent-watch/index.html"
        safe = posixpath.normpath(path).lstrip("/")
        if safe.startswith(".."):
            return self._err(403, "forbidden")
        f = (config.WEB / safe).resolve()
        # audit: normpath alone still followed a symlink placed under web/
        if not str(f).startswith(str(config.WEB.resolve()) + "/"):
            return self._err(403, "forbidden")
        if not f.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        data = f.read_bytes()
        compressible = len(data) >= _GZIP_MIN and ctype.startswith(_GZIP_TYPES)
        gz = compressible and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            data = _gzipped(f, data)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if compressible:
            self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        # no-store: dashboards get iterated constantly; a tab resurrected
        # from bfcache running yesterday's JS caused stale prompt() flows
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self._write(data)


def _safe_to_bind(cfg: dict, auth_enabled: bool) -> tuple[bool, str]:
    """Decide whether the (host, auth) combination is allowed to start.

    Loopback binds always boot (solo-dev UX: `python3 -m app` with no
    config). A non-loopback bind requires at least one of:
      * a configured bearer token (`auth_token` / `LIMCARS_TOKEN`), or
      * a password already set in data/auth.json.
    This is the pre-flight check that keeps a brand new install from
    exposing arbitrary command execution to the whole LAN.
    """
    host = cfg.get("host", "")
    if config.is_loopback(host):
        return True, ""
    if cfg.get("auth_token"):
        return True, ""
    if auth_enabled:
        return True, ""
    return False, (
        f"refusing to bind non-loopback host {host!r} with no authentication "
        "configured.\n"
        "Pick one and retry:\n"
        "  * export LIMCARS_HOST=127.0.0.1   (localhost only — the default)\n"
        "  * export LIMCARS_TOKEN=<secret>   (bearer auth, scripts friendly)\n"
        "  * start on 127.0.0.1 once, open the Security panel, set an admin "
        "password, then rebind to the LAN address."
    )


def serve(cfg: dict) -> None:
    app = Limcars(cfg)
    ok, msg = _safe_to_bind(cfg, app.auth.enabled)
    if not ok:
        raise SystemExit(f"[limcars-next] {msg}")
    Handler.app = app
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True
    # Boot banner: tell the user exactly which security posture they got, so
    # a quiet "LAN-wide no-auth install" cannot slip past unnoticed.
    posture = []
    if cfg.get("auth_token"):
        posture.append("bearer-token")
    if app.auth.enabled:
        posture.append("password")
    if cfg.get("allow_exec"):
        posture.append("ALLOW_EXEC=on")
    if not posture:
        posture.append("open (loopback-only)")
    print(f"[limcars-next] serving on http://{cfg['host']}:{cfg['port']}  "
          f"[{', '.join(posture)}]  "
          f"(engines: {[s.id for s in app.engines.all()]})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in app.engines.all():
            s.stop()
        app.hw.stop()
        for n in app.nodes:
            n.stop()
        if hasattr(app, "status_tiles"):
            app.status_tiles.stop()
