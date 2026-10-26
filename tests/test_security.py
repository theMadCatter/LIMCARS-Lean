"""Integration tests for the secure-by-default surface.

Spins the real server up on an ephemeral port against a scratch data
directory, pokes it through plain `urllib`, and asserts the four security
invariants we care about most:

    * default install exposes NO command-executing route (`allow_exec`
      master switch is off);
    * non-loopback bind refuses to start when no auth is configured;
    * when auth is required, every API/page route 401s/303s without a
      session cookie or bearer token;
    * the admin password still gates every exec route when the master
      switch is on, and lockout + sliding-window sessions behave.

These tests build the HTTP server with Python's own http.server stack —
they share a real `Limcars` app with the production code path so a
future change that breaks the gate also breaks a test. They never spawn
subprocesses or write anywhere outside the tmp_path pytest hands them.
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app import config, server


# --------------------------------------------------------------------- helpers


def _free_port() -> int:
    """Grab a free TCP port from the OS. Avoids hard-coding 8951 so parallel
    pytest invocations don't collide."""
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mkcfg(tmp_path, **overrides) -> dict:
    """Build a config dict pointing every path at tmp_path, so no real
    data/*.db / data/auth.json ever gets touched by a test."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = dict(config.DEFAULTS)
    cfg.update({
        "host": "127.0.0.1",
        "port": _free_port(),
        "db_path": str(data / "limcars.db"),
        "auth_path": str(data / "auth.json"),
        "settings_path": str(data / "settings.json"),
        "recipes_dir": str(data),
        "engines": [],       # no sampler threads trying to reach 127.0.0.1:8000
        "services": [],      # no shelling out
        "allow_exec": False,
    })
    cfg.update(overrides)
    return cfg


class _ServerFixture:
    """Boot an app + handler on a background thread, hand the test a URL."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.app = server.Limcars(cfg)
        # Install the shared app on a fresh Handler subclass so each fixture
        # has its own class attribute (otherwise two tests racing each other
        # would clobber server.Handler.app).
        self.Handler = type("H", (server.Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), self.Handler)
        self.httpd.daemon_threads = True
        self._th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._th.start()
        self.base = f"http://{cfg['host']}:{cfg['port']}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        for s in self.app.engines.all():
            s.stop()
        self.app.hw.stop()
        for n in self.app.nodes:
            n.stop()
        if hasattr(self.app, "clients"):
            self.app.clients.stop()


@pytest.fixture
def srv(tmp_path, request):
    """Factory fixture: `srv(cfg_overrides…)` returns a running server."""
    created: list[_ServerFixture] = []

    def _mk(**overrides):
        f = _ServerFixture(_mkcfg(tmp_path, **overrides))
        created.append(f)
        return f

    yield _mk
    for f in created:
        f.stop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn 3xx responses into HTTPError so the test can inspect the
    Location header instead of silently following the redirect."""

    def http_error_303(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_301 = http_error_302 = http_error_307 = http_error_308 = http_error_303


_OPENER = urllib.request.build_opener(_NoRedirect())


def _get(url: str, headers: dict | None = None, cookie: str | None = None
         ) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with _OPENER.open(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _post(url: str, body: dict | None = None, headers: dict | None = None,
          cookie: str | None = None) -> tuple[int, bytes, dict]:
    data = json.dumps(body or {}).encode()
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _cookie_from(hdrs: dict) -> str | None:
    """Pull just the `name=value` of our session cookie out of a Set-Cookie."""
    raw = hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or ""
    for part in raw.split(","):
        kv = part.strip().split(";", 1)[0]
        if kv.startswith(server.SESSION_COOKIE + "="):
            return kv
    return None


# --------------------------------------------------------------------- tests


class TestLoopbackBindGuard:
    """`serve()` must refuse to bind a non-loopback host unless auth is set."""

    def test_loopback_bind_is_safe_without_auth(self, tmp_path):
        cfg = _mkcfg(tmp_path, host="127.0.0.1")
        app = server.Limcars(cfg)
        try:
            ok, msg = server._safe_to_bind(cfg, app.auth.enabled)
            assert ok is True and msg == ""
        finally:
            app.hw.stop()
            if hasattr(app, "clients"):
                app.clients.stop()

    def test_nonloopback_without_auth_refuses(self, tmp_path):
        cfg = _mkcfg(tmp_path, host="0.0.0.0")
        app = server.Limcars(cfg)
        try:
            ok, msg = server._safe_to_bind(cfg, app.auth.enabled)
            assert ok is False
            assert "no authentication configured" in msg
        finally:
            app.hw.stop()
            if hasattr(app, "clients"):
                app.clients.stop()

    def test_nonloopback_with_bearer_boots(self, tmp_path):
        cfg = _mkcfg(tmp_path, host="0.0.0.0", auth_token="tok")
        app = server.Limcars(cfg)
        try:
            ok, msg = server._safe_to_bind(cfg, app.auth.enabled)
            assert ok is True
        finally:
            app.hw.stop()
            if hasattr(app, "clients"):
                app.clients.stop()

    def test_nonloopback_with_password_boots(self, tmp_path):
        cfg = _mkcfg(tmp_path, host="0.0.0.0")
        app = server.Limcars(cfg)
        try:
            app.auth.set_password("beach123")
            ok, _ = server._safe_to_bind(cfg, app.auth.enabled)
            assert ok is True
        finally:
            app.hw.stop()
            if hasattr(app, "clients"):
                app.clients.stop()

    def test_is_loopback_helper(self):
        assert config.is_loopback("127.0.0.1")
        assert config.is_loopback("127.0.0.5")   # 127/8 is all loopback
        assert config.is_loopback("::1")
        assert config.is_loopback("localhost")
        assert not config.is_loopback("0.0.0.0")
        assert not config.is_loopback("192.168.1.5")
        assert not config.is_loopback("::")


class TestLoopbackNoAuthDefault:
    """A loopback install with no token and no password stays zero-config
    for read-only routes. Exec routes are still off by default."""

    def test_health_public(self, srv):
        f = srv()
        code, body, _ = _get(f.base + "/api/health")
        assert code == 200
        assert json.loads(body)["ok"] is True

    def test_snapshot_open(self, srv):
        f = srv()
        code, _, _ = _get(f.base + "/api/snapshot")
        assert code == 200

    def test_recipe_add_blocked_by_master_switch(self, srv):
        f = srv()
        code, body, _ = _post(f.base + "/api/recipes",
                              {"name": "x", "cmd": "echo hi"})
        assert code == 403
        assert b"command execution disabled" in body

    def test_recipe_start_blocked_by_master_switch(self, srv):
        f = srv()
        code, body, _ = _post(f.base + "/api/recipes/x/start")
        assert code == 403
        assert b"command execution disabled" in body

    def test_recipe_stop_blocked_by_master_switch(self, srv):
        f = srv()
        code, _, _ = _post(f.base + "/api/recipes/x/stop")
        assert code == 403

    def test_services_blocked_by_master_switch(self, srv):
        f = srv()
        code, _, _ = _post(f.base + "/api/services/foo/start")
        assert code == 403

    def test_engines_live_stop_blocked(self, srv):
        f = srv()
        code, _, _ = _post(f.base + "/api/engines-live/9999/stop")
        assert code == 403

    def test_probe_blocked(self, srv):
        f = srv()
        code, _, _ = _post(f.base + "/api/probe")
        assert code == 403


class TestAuthRequiredGate:
    """When a token or password is configured, EVERY non-public route needs
    credentials — read AND write."""

    def test_dashboard_redirects_without_auth(self, srv):
        f = srv(auth_token="tok")
        code, _, hdrs = _get(f.base + "/")
        assert code == 303
        assert hdrs.get("Location") == "/login"

    def test_snapshot_401_without_auth(self, srv):
        f = srv(auth_token="tok")
        code, _, _ = _get(f.base + "/api/snapshot")
        assert code == 401

    def test_login_page_is_public(self, srv):
        f = srv(auth_token="tok")
        code, body, _ = _get(f.base + "/login")
        assert code == 200
        assert b"Admin password" in body

    def test_health_still_public_when_auth_on(self, srv):
        f = srv(auth_token="tok")
        code, _, _ = _get(f.base + "/api/health")
        assert code == 200

    def test_bearer_header_grants_access(self, srv):
        f = srv(auth_token="tok")
        code, _, _ = _get(f.base + "/api/snapshot",
                          headers={"X-Limcars-Token": "tok"})
        assert code == 200

    def test_wrong_bearer_denied(self, srv):
        f = srv(auth_token="tok")
        code, _, _ = _get(f.base + "/api/snapshot",
                          headers={"X-Limcars-Token": "wrong"})
        assert code == 401

    def test_cookie_login_round_trip(self, srv):
        f = srv(auth_token="tok")
        code, _, hdrs = _post(f.base + "/api/auth/login", {"token": "tok"})
        assert code == 200
        cookie = _cookie_from(hdrs)
        assert cookie and cookie.startswith(server.SESSION_COOKIE + "=")
        code, _, _ = _get(f.base + "/api/snapshot", cookie=cookie)
        assert code == 200

    def test_cookie_is_httponly_and_strict(self, srv):
        f = srv(auth_token="tok")
        _, _, hdrs = _post(f.base + "/api/auth/login", {"token": "tok"})
        raw = hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or ""
        assert "HttpOnly" in raw
        assert "SameSite=Strict" in raw

    def test_logout_invalidates_session(self, srv):
        f = srv(auth_token="tok")
        _, _, hdrs = _post(f.base + "/api/auth/login", {"token": "tok"})
        cookie = _cookie_from(hdrs)
        _post(f.base + "/api/auth/logout", cookie=cookie)
        code, _, _ = _get(f.base + "/api/snapshot", cookie=cookie)
        assert code == 401

    def test_password_change_invalidates_sessions(self, srv):
        f = srv()
        f.app.auth.set_password("pw1")
        _, _, hdrs = _post(f.base + "/api/auth/login", {"password": "pw1"})
        cookie = _cookie_from(hdrs)
        assert cookie is not None
        code, _, _ = _get(f.base + "/api/snapshot", cookie=cookie)
        assert code == 200
        # Rotate the password; the old cookie must stop working.
        f.app.auth.set_password("pw2", current="pw1")
        code, _, _ = _get(f.base + "/api/snapshot", cookie=cookie)
        assert code == 401


class TestPasswordGatedExec:
    """When `allow_exec` is on, every exec/mutating route still needs the
    admin password (lockout ladder shared with recipe writes). Localhost
    is no longer exempt."""

    def test_recipe_add_requires_password_when_set(self, srv):
        f = srv(allow_exec=True, auth_token="tok")
        f.app.auth.set_password("beach")
        headers = {"X-Limcars-Token": "tok"}
        # No X-Limcars-Pw header: 401 even though the token is valid.
        code, _, _ = _post(f.base + "/api/recipes",
                           {"name": "x", "cmd": "echo hi"}, headers=headers)
        assert code == 401
        # Wrong password still 401.
        code, _, _ = _post(f.base + "/api/recipes",
                           {"name": "x", "cmd": "echo hi"},
                           headers={**headers, "X-Limcars-Pw": "nope"})
        assert code == 401
        # Correct password lets the add through (returns {ok: True}).
        code, body, _ = _post(f.base + "/api/recipes",
                              {"name": "echo-ok", "cmd": "echo ok"},
                              headers={**headers, "X-Limcars-Pw": "beach"})
        assert code == 200
        assert json.loads(body)["ok"] is True

    def test_probe_requires_password_when_set(self, srv):
        f = srv(allow_exec=True, auth_token="tok")
        f.app.auth.set_password("beach")
        headers = {"X-Limcars-Token": "tok"}
        code, _, _ = _post(f.base + "/api/probe", headers=headers)
        assert code == 401
        code, _, _ = _post(f.base + "/api/probe",
                           headers={**headers, "X-Limcars-Pw": "beach"})
        # No engine configured for this test => 400, but the auth gate passed.
        assert code == 400

    def test_services_requires_password(self, srv):
        f = srv(allow_exec=True, auth_token="tok")
        f.app.auth.set_password("beach")
        code, _, _ = _post(f.base + "/api/services/foo/start",
                           headers={"X-Limcars-Token": "tok"})
        assert code == 401

    def test_allow_exec_without_password_still_refuses(self, srv):
        """Opt-in exec with NO password and NO bearer must still refuse —
        otherwise a half-configured install exposes the full RCE surface
        the moment the admin flips allow_exec on."""
        f = srv(allow_exec=True)
        code, body, _ = _post(f.base + "/api/recipes",
                              {"name": "x", "cmd": "echo hi"})
        assert code == 403
        assert b"set an admin password" in body

    def test_lockout_ladder_through_login(self, srv):
        f = srv()
        f.app.auth.set_password("beach")
        last = None
        for _ in range(3):
            last = _post(f.base + "/api/auth/login", {"password": "nope"})
        # Third bad try trips the 60 s tier → 423 with retry_after.
        assert last is not None and last[0] == 423
        body = json.loads(last[1])
        assert body["error"] == "locked"
        assert body["retry_after"] > 50


class TestConfigPatchHardened:
    """The POST /api/config whitelist deliberately does NOT include
    `allow_exec` — flipping the master exec switch must stay an explicit
    disk edit (or env var), not something a compromised session can toggle."""

    def test_allow_exec_not_patchable(self, srv):
        f = srv(auth_token="tok")
        code, body, _ = _post(f.base + "/api/config",
                              {"allow_exec": True},
                              headers={"X-Limcars-Token": "tok"})
        assert code == 400
        assert b"only" in body        # the "only [...] are editable" message
        assert f.app.cfg["allow_exec"] is False
