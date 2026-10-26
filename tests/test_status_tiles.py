"""Status tiles — ComfyUI /queue + /system_stats and generic HTTP health.

Covers the three states the sampler reports:
  * up    : target responded with a 2xx and the probe parsed
  * down  : target refused the connection or returned a non-OK status
  * stale : target has never been polled yet (initial state)

ComfyUI is special-cased because the owner uses it on Chief; we assert
that queue counts and VRAM figures flow through from /system_stats.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.status_tiles import StatusTileSampler


class _StubHandler(BaseHTTPRequestHandler):
    """Backed by `_responses` class attribute: path -> (status, body)."""

    responses: dict = {}

    def log_message(self, *_a, **_kw):
        pass

    def do_GET(self):  # noqa: N802
        if self.path in self.responses:
            status, body = self.responses[self.path]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


@pytest.fixture
def stub():
    """Boot a stub HTTP server; return (base_url, response_setter)."""
    _StubHandler.responses = {}
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    httpd = HTTPServer(("127.0.0.1", port), _StubHandler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True); th.start()
    try:
        yield f"http://127.0.0.1:{port}", _StubHandler
    finally:
        httpd.shutdown()
        httpd.server_close()


def _wait_until(predicate, timeout=3.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class TestGenericHttpTile:
    def test_up_when_status_ok(self, stub):
        base, h = stub
        h.responses["/health"] = (200, b'{"status":"ok"}')
        s = StatusTileSampler({
            "status_tiles": [{"id": "kokoro", "label": "Kokoro TTS",
                              "type": "http", "url": base + "/health"}],
            "status_tiles_interval_s": 60,
        })
        s.start()
        try:
            assert _wait_until(
                lambda: s.snapshot()[0]["up"] is True)
            row = s.snapshot()[0]
            assert row["id"] == "kokoro"
            assert row["info"]["status"] == 200
            assert row["latency_ms"] is not None
            assert row["error"] is None
        finally:
            s.stop()

    def test_down_when_connection_refused(self, tmp_path):
        """Point the tile at a port with no listener; sampler must mark
        it down and still produce a snapshot row (not throw)."""
        s = StatusTileSampler({
            "status_tiles": [{"id": "nope", "label": "nope",
                              "type": "http",
                              "url": "http://127.0.0.1:1"}],
            "status_tiles_interval_s": 60,
        })
        s.start()
        try:
            assert _wait_until(
                lambda: s.snapshot()[0]["up"] is False)
            row = s.snapshot()[0]
            assert row["error"]      # some descriptive error string
        finally:
            s.stop()

    def test_custom_ok_status(self, stub):
        """A target that answers 204 is still considered up when the
        tile's ok_status list says so."""
        base, h = stub
        h.responses["/ping"] = (204, b"")
        s = StatusTileSampler({
            "status_tiles": [{"id": "ping", "label": "ping", "type": "http",
                              "url": base + "/ping",
                              "ok_status": [200, 204]}],
            "status_tiles_interval_s": 60,
        })
        s.start()
        try:
            assert _wait_until(lambda: s.snapshot()[0]["up"] is True)
        finally:
            s.stop()


class TestComfyuiTile:
    def test_up_parses_queue_and_vram(self, stub):
        base, h = stub
        h.responses["/queue"] = (200, json.dumps({
            "queue_running": [{"a": 1}],
            "queue_pending": [{"b": 1}, {"c": 2}, {"d": 3}],
        }).encode())
        h.responses["/system_stats"] = (200, json.dumps({
            "system": {"comfyui_version": "0.3.14"},
            "devices": [{"vram_total": 24 * 1024**3,
                         "vram_free":  18 * 1024**3}],
        }).encode())
        s = StatusTileSampler({
            "status_tiles": [{"id": "comfy", "label": "ComfyUI",
                              "type": "comfyui", "url": base}],
            "status_tiles_interval_s": 60,
        })
        s.start()
        try:
            assert _wait_until(lambda: s.snapshot()[0]["up"] is True)
            info = s.snapshot()[0]["info"]
            assert info["running"] == 1
            assert info["pending"] == 3
            assert info["vram_total"] == 24 * 1024**3
            assert info["vram_free"] == 18 * 1024**3
            assert info["version"] == "0.3.14"
        finally:
            s.stop()

    def test_down_when_one_endpoint_fails(self, stub):
        """ComfyUI needs BOTH /queue and /system_stats; a 500 on either
        marks the tile down."""
        base, h = stub
        h.responses["/queue"] = (200, b'{"queue_running":[],"queue_pending":[]}')
        h.responses["/system_stats"] = (500, b"boom")
        s = StatusTileSampler({
            "status_tiles": [{"id": "comfy", "type": "comfyui",
                              "label": "ComfyUI", "url": base}],
            "status_tiles_interval_s": 60,
        })
        s.start()
        try:
            assert _wait_until(lambda: s.snapshot()[0]["up"] is False)
        finally:
            s.stop()


class TestNoTilesConfigured:
    def test_empty_config_runs_no_thread(self):
        s = StatusTileSampler({})
        s.start()
        time.sleep(0.1)
        s.stop()
        assert s.snapshot() == []
