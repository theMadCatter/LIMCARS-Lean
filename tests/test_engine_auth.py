"""Per-engine auth headers / API keys.

Covers the three ways to configure the credential (literal `api_key`,
env-var reference `api_key_env`, free-form `headers` map), the fallback
header name (`auth_header`), and the fact that neither path logs the
secret anywhere observable from a test.

The HTTP layer is exercised end-to-end against a tiny stdlib HTTP server
so no engine config path escapes the test as untested string plumbing.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import engine_info, probe
from app.http_client import engine_headers


# --------------------------------------------------------------------- unit


class TestEngineHeaders:
    def test_no_key_set_returns_only_ua(self):
        hdrs = engine_headers({})
        assert hdrs.get("User-Agent", "").startswith("limcars-next")
        assert "Authorization" not in hdrs

    def test_literal_api_key_becomes_bearer(self):
        hdrs = engine_headers({"api_key": "sk-foo"})
        assert hdrs["Authorization"] == "Bearer sk-foo"

    def test_env_var_lookup(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY_X", "sk-bar")
        hdrs = engine_headers({"api_key_env": "TEST_KEY_X"})
        assert hdrs["Authorization"] == "Bearer sk-bar"

    def test_missing_env_var_adds_no_header(self, monkeypatch):
        monkeypatch.delenv("ABSENT_KEY", raising=False)
        hdrs = engine_headers({"api_key_env": "ABSENT_KEY"})
        assert "Authorization" not in hdrs

    def test_literal_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY_X", "sk-env")
        hdrs = engine_headers({"api_key": "sk-literal",
                               "api_key_env": "TEST_KEY_X"})
        assert hdrs["Authorization"] == "Bearer sk-literal"

    def test_custom_auth_header_name_skips_bearer_prefix(self):
        hdrs = engine_headers({"api_key": "sk-foo",
                               "auth_header": "X-API-Key"})
        assert hdrs["X-API-Key"] == "sk-foo"
        assert "Authorization" not in hdrs

    def test_headers_map_augments_everything(self):
        hdrs = engine_headers({"api_key": "sk-foo",
                               "headers": {"X-Trace": "abc"}})
        assert hdrs["Authorization"] == "Bearer sk-foo"
        assert hdrs["X-Trace"] == "abc"

    def test_headers_map_overrides_authorization(self):
        hdrs = engine_headers({"api_key": "sk-foo",
                               "headers": {"Authorization": "Basic xxx"}})
        assert hdrs["Authorization"] == "Basic xxx"


# --------------------------------------------------------------------- integration


class _CollectingHandler(BaseHTTPRequestHandler):
    """Records the Authorization header per path and answers a canned
    /metrics / /v1/models / /version body so the real info() code path
    does not blow up on parsing."""

    seen: dict = {}

    def log_message(self, *_args, **_kw):
        pass

    def _record(self):
        self.__class__.seen[self.path] = {
            "Authorization": self.headers.get("Authorization"),
            "X-API-Key": self.headers.get("X-API-Key"),
            "X-Trace": self.headers.get("X-Trace"),
        }

    def do_GET(self):  # noqa: N802
        self._record()
        if self.path == "/metrics":
            body = (b'# HELP vllm:cache_config_info x\n'
                    b'vllm:cache_config_info{block_size="16",num_gpu_blocks="2000",'
                    b'kv_cache_size_tokens="32000",max_model_len="4096",'
                    b'cache_dtype="auto"} 1.0\n')
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "demo/model"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        if self.path == "/version":
            body = b'{"version":"0.9.9"}'
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        self.send_response(404); self.end_headers()


@pytest.fixture
def stub_engine():
    """Boot a canned stub engine on an ephemeral port."""
    _CollectingHandler.seen = {}
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = HTTPServer(("127.0.0.1", port), _CollectingHandler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{port}", _CollectingHandler
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestInfoCarriesAuth:
    def test_bearer_applied_on_every_fetch(self, stub_engine):
        url, h = stub_engine
        out = engine_info.info(url, {"api_key": "sk-info"})
        assert out["healthy"] is True
        for p in ("/metrics", "/v1/models", "/version"):
            assert h.seen[p]["Authorization"] == "Bearer sk-info"

    def test_no_eng_still_works(self, stub_engine):
        url, h = stub_engine
        out = engine_info.info(url)
        assert out["healthy"] is True
        assert h.seen["/metrics"]["Authorization"] is None


class TestProbeCarriesAuth:
    def test_probe_sends_bearer_header(self, stub_engine, monkeypatch):
        """The real probe() opens a streamed chat/completions request against
        the engine. We replace urlopen with a recorder — the behaviour we
        care about is that the Authorization header made it onto the
        Request object."""
        captured = {}
        import app.probe as probe_mod
        from io import BytesIO

        class FakeResponse:
            def __init__(self):
                self._lines = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
                               b'data: {"usage":{"prompt_tokens":5,"completion_tokens":1}}\n',
                               b'data: [DONE]\n']

            def __iter__(self):
                return iter(self._lines)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return FakeResponse()

        monkeypatch.setattr(probe_mod.urllib.request, "urlopen", fake_urlopen)
        res = probe_mod.probe("http://ignored", "demo",
                              eng={"api_key": "sk-probe"})
        assert res["ok"] is True
        # urllib normalises header casing; be case-insensitive.
        hdrs = {k.lower(): v for k, v in captured["headers"].items()}
        assert hdrs.get("authorization") == "Bearer sk-probe"
