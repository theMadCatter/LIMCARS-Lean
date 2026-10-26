"""Configuration: defaults < data/config.json < LIMCARS_* env overrides."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"

# Security defaults (changed in the "secure by default" PR):
#   * host  : 127.0.0.1 — any LAN exposure is now opt-in, since a stock
#             install exposed every API to the whole subnet.
#   * allow_exec : False — recipes, services, live-engine kill and the
#             engine probe all RUN commands/shell lines. Keeping them off
#             by default means a mis-set password or a rogue request can
#             never spawn a process; the user explicitly turns the
#             feature on when they want it.
#   * auth_token : None  — unchanged default (bearer auth stays optional),
#             but server.serve() now refuses to bind a non-loopback host
#             when NO authentication (bearer or admin password) exists.
DEFAULTS = {
    "port": 8951,
    "host": "127.0.0.1",
    "live_hz": 2.0,          # engine sampler ticks per second (1-4)
    "hw_hz": 1.0,            # hardware sampler ticks per second
    "ring_seconds": 3900,    # in-memory ring depth (65 min — supports the 1 h window)
    "persist_seconds": 604800, # sqlite downsample depth (1 s resolution, 7 d)
    "window_s": 30.0,        # window for p50 / avg derived stats
    "pricing": None,         # None -> resolve model against data/pricing.json
    "engines": [
        {"id": "flashnext", "url": "http://127.0.0.1:8000", "kind": None,
         "label": "Qwen3.8-Flash-Next"},
    ],
    "services": [  # systemd units the admin page may start/stop
        {"id": "flashnext", "unit": "docker", "label": "Flash-Next engine (docker)",
         "start": "docker start rl-flashnext-nvidia-vr8vr8",
         "stop": "docker stop rl-flashnext-nvidia-vr8vr8",
         "status": "docker ps --filter name=rl-flashnext-nvidia-vr8vr8 --format {{.Status}}"},
    ],
    "db_path": str(DATA / "limcars.db"),
    "auth_token": None,      # when set, /api/* and the UI accept the bearer
    "allow_exec": False,     # master switch for every command-executing route
    # Session cookie lifetime for the browser login (seconds). 12 h matches
    # the typical "stay logged in through a workday" expectation; the backing
    # tokens are regenerated across restarts (nothing to steal from disk).
    "session_ttl_s": 43200,
}


# Loopback addresses allowed to run without authentication. IPv4 "127.*/8"
# plus IPv6 "::1" cover every ordinary localhost binding; anything else
# (including "0.0.0.0" and real NIC addresses) must opt into exposure.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "ip6-localhost",
                   "ip6-loopback"}


def is_loopback(host: str) -> bool:
    """Return True when `host` only accepts connections from the local box.

    `0.0.0.0` and `::` listen on EVERY interface and must NOT be treated as
    loopback — the server uses this helper to decide whether an auth-less
    boot is safe.
    """
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load() -> dict:
    cfg = dict(DEFAULTS)
    f = DATA / "config.json"
    if f.exists():
        try:
            cfg.update(json.loads(f.read_text()))
        except Exception as exc:  # noqa: BLE001 — bad config must not brick boot
            print(f"[config] ignoring {f}: {exc}")
    if v := os.environ.get("LIMCARS_PORT"):
        cfg["port"] = int(v)
    if v := os.environ.get("LIMCARS_HOST"):
        cfg["host"] = v
    if v := os.environ.get("LIMCARS_DB"):
        cfg["db_path"] = v
    if v := os.environ.get("LIMCARS_TOKEN"):
        cfg["auth_token"] = v
    if v := os.environ.get("LIMCARS_ALLOW_EXEC"):
        # Any non-empty value except "0"/"false"/"no" (case-insensitive)
        # enables the master switch. Keeping it env-settable makes the
        # systemd unit a one-liner on trusted hosts.
        cfg["allow_exec"] = v.strip().lower() not in ("", "0", "false", "no", "off")
    # audit F-9: `data/` holds the sqlite db (every token the sampler has
    # seen), the admin password hash, and the recipe commands. 0o700 keeps
    # it readable only by the service user; the files inside already go
    # 0o600 individually, but a leaky umask on an older install can still
    # leave the DIR 0o755 — fix it on every boot, cheap and idempotent.
    DATA.mkdir(exist_ok=True)
    try:
        os.chmod(DATA, 0o700)
    except OSError:
        pass
    return cfg
