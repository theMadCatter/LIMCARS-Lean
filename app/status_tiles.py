"""Status tiles for ancillary services.

The owner's homelab has moving parts beyond the inference engine itself:
a ComfyUI instance for image gen on Chief, a Kokoro TTS service on
:8880, a LiteLLM proxy on :4000. None of them emit the Prometheus
counters the sampler relies on, but all of them do answer HTTP, and
"is it up?" is 90% of what the owner wants to see at a glance.

Design:

* `StatusTileSampler` runs ONE background thread that polls every tile
  on `interval_s` cadence. Each tile is a dict from
  `cfg["status_tiles"]`:

      { "id": "comfyui",
        "type": "comfyui" | "http",
        "url":  "http://127.0.0.1:8188",
        "label": "ComfyUI",
        "headers": { ... }      # optional, same shape as engine headers
        "timeout": 3.0,         # per-request, default 3 s
        "ok_status": [200]      # http type only; status codes counted up
      }

* Two tile types, both deliberately boring:

  * `http` — GET the URL, record HTTP status + latency_ms. Up when the
    status is in `ok_status` (default: 200).

  * `comfyui` — GET `<url>/queue` and `<url>/system_stats`; expose
    queue length, running count, and VRAM free/total if the server
    reports it. "Up" means both endpoints answered 200.

* Snapshot is a plain dict served over `/api/status-tiles`. Serialised
  rows carry nothing more than name/type/up/latency/info/error, so a
  misbehaving tile cannot leak a secret into the UI.

Nothing here spawns a process or runs a shell command — these tiles are
read-only observers, independent from the `allow_exec` gate.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_INTERVAL_S = 15.0
DEFAULT_TIMEOUT_S = 3.0


def _tile_headers(tile: dict) -> dict[str, str]:
    """Build HTTP headers for one tile. Lives here (rather than reusing
    http_client.engine_headers) so this module stays usable on branches
    that don't ship the per-engine-auth refactor yet."""
    headers: dict[str, str] = {"User-Agent": "limcars-status/1.0"}
    import os
    key = None
    if tile.get("api_key"):
        key = str(tile["api_key"])
    elif tile.get("api_key_env"):
        key = os.environ.get(str(tile["api_key_env"])) or None
    if key:
        name = str(tile.get("auth_header") or "Authorization")
        headers[name] = f"Bearer {key}" if name.lower() == "authorization" else key
    extra = tile.get("headers") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and v is not None:
                headers[str(k)] = str(v)
    return headers


class StatusTileSampler:
    def __init__(self, cfg: dict):
        self.tiles = list(cfg.get("status_tiles") or [])
        self.interval = float(cfg.get("status_tiles_interval_s",
                                      DEFAULT_INTERVAL_S))
        self._lock = threading.Lock()
        # id -> last snapshot dict. The sampler publishes a fresh dict each
        # tick; readers always see a complete tile row (never a partial
        # mid-update value).
        self._state: dict[str, dict] = {t.get("id") or f"tile-{i}": {
            "id": t.get("id") or f"tile-{i}",
            "label": t.get("label") or t.get("id") or t.get("url"),
            "type": t.get("type") or "http",
            "url": t.get("url"),
            "up": None,
            "checked_at": 0.0,
            "latency_ms": None,
            "info": {},
            "error": None,
        } for i, t in enumerate(self.tiles)}
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        if not self.tiles:
            return
        self._th = threading.Thread(target=self._loop, daemon=True,
                                    name="status-tiles")
        self._th.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._state.values()]

    # ---- internals ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            for t in self.tiles:
                try:
                    self._probe(t)
                except Exception as exc:  # noqa: BLE001 — never kill the thread
                    tid = t.get("id") or "?"
                    with self._lock:
                        self._state[tid] = {
                            **self._state.get(tid, {}),
                            "up": False, "checked_at": time.time(),
                            "latency_ms": None,
                            "error": str(exc)[:160], "info": {}}
            # Jittered sleep so N tiles don't all retry on the same ms
            # after a transient outage.
            self._stop.wait(self.interval)

    def _get(self, url: str, headers: dict[str, str], timeout: float
             ) -> tuple[int, bytes, float]:
        req = urllib.request.Request(url, headers=headers)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                body = r.read(1 << 20)   # cap at 1 MiB — a status probe is tiny
                return r.status, body, (time.perf_counter() - t0) * 1000
        except urllib.error.HTTPError as e:
            return e.code, b"", (time.perf_counter() - t0) * 1000

    def _probe(self, t: dict) -> None:
        tid = t.get("id") or "?"
        url = t.get("url") or ""
        if not url:
            return
        headers = _tile_headers(t)
        timeout = float(t.get("timeout") or DEFAULT_TIMEOUT_S)
        kind = (t.get("type") or "http").lower()
        if kind == "comfyui":
            info, up, err, latency = self._probe_comfyui(url, headers, timeout)
        else:
            info, up, err, latency = self._probe_http(url, headers, timeout, t)
        with self._lock:
            self._state[tid] = {
                "id": tid,
                "label": t.get("label") or tid,
                "type": kind,
                "url": url,
                "up": up,
                "checked_at": time.time(),
                "latency_ms": round(latency, 1) if latency is not None else None,
                "info": info,
                "error": err,
            }

    def _probe_http(self, url: str, headers: dict, timeout: float,
                    tile: dict) -> tuple[dict, bool, str | None, float | None]:
        ok_statuses = set(tile.get("ok_status") or [200])
        try:
            status, body, latency = self._get(url, headers, timeout)
        except Exception as exc:  # noqa: BLE001
            return {}, False, str(exc)[:160], None
        info: dict[str, Any] = {"status": status}
        # Try to parse the body as JSON for a one-liner "info" summary;
        # fall through to a plain text snippet otherwise. Either way,
        # cap what we store so a huge body cannot bloat the SSE snapshot.
        snippet = body[:200].decode("utf-8", "replace") if body else ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                info["summary"] = {k: parsed[k] for k in list(parsed)[:4]
                                   if isinstance(parsed[k],
                                                 (str, int, float, bool))}
            else:
                info["summary"] = snippet.strip()
        except (ValueError, UnicodeError):
            info["summary"] = snippet.strip()
        return info, status in ok_statuses, None, latency

    def _probe_comfyui(self, url: str, headers: dict, timeout: float
                       ) -> tuple[dict, bool, str | None, float | None]:
        base = url.rstrip("/")
        info: dict[str, Any] = {}
        latency = None
        try:
            qs, qb, qt = self._get(base + "/queue", headers, timeout)
            latency = qt
            if qs == 200 and qb:
                qd = json.loads(qb)
                # ComfyUI's /queue returns {queue_running: [...], queue_pending: [...]}
                info["running"] = len(qd.get("queue_running") or [])
                info["pending"] = len(qd.get("queue_pending") or [])
            ss, sb, _ = self._get(base + "/system_stats", headers, timeout)
            if ss == 200 and sb:
                sd = json.loads(sb)
                # ComfyUI's /system_stats shape varies by version, but the
                # 'devices' array consistently carries vram_free/vram_total.
                devices = sd.get("devices") or []
                if devices:
                    d0 = devices[0]
                    for key in ("vram_free", "vram_total",
                                "torch_vram_free", "torch_vram_total"):
                        if key in d0:
                            info[key] = int(d0[key])
                sys_ = sd.get("system") or {}
                if "comfyui_version" in sys_:
                    info["version"] = sys_["comfyui_version"]
            up = (qs == 200) and (ss == 200)
            err = None if up else f"queue={qs} system_stats={ss}"
            return info, up, err, latency
        except Exception as exc:  # noqa: BLE001
            return info, False, str(exc)[:160], latency
