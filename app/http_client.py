"""Shared helpers for talking to inference-engine HTTP APIs.

The sampler, the engine-info panel, the probe and the live-engine
fingerprinter all open plain `urllib.request` calls against a user-supplied
`http(s)://host:port` URL. Previously those calls carried only a
`User-Agent` header — so an engine protected by an API key (vLLM
`--api-key`, LiteLLM master key, any front-door reverse proxy) answered
every poll with a 401 and the dashboard silently went dark.

This module centralises:

* `engine_headers(eng, cfg, extra_env=…)` — pulls the per-engine
  auth from the engine config dict (`api_key`, `headers`,
  `auth_header`, `api_key_env`) and returns the final HTTP header dict
  that every HTTP call should attach. Nothing here is logged (the key
  stays inside the dict); callers must not stringify the headers.
* `make_request(url, …, headers=…)` — thin convenience wrapper that
  attaches the User-Agent and (optional) auth headers in one place.

Only two inputs ever come from the environment, both opt-in:

    engine.api_key        : literal key, e.g. "sk-foo"
    engine.api_key_env    : name of an env var holding the key — this is
                            the recommended path, since the config file
                            is on disk and may be committed by accident
    engine.auth_header    : header NAME to carry the key, default
                            "Authorization: Bearer <key>" (openai style).
    engine.headers        : flat {name: value} map applied last.
                            Overrides anything from api_key/auth_header.

The dict is built fresh per request — never cached — so a rotating env
var is picked up on the next tick without a daemon restart.
"""
from __future__ import annotations

import os
import urllib.request
from typing import Mapping


def engine_headers(eng: Mapping, cfg: Mapping | None = None) -> dict[str, str]:
    """Return the HTTP headers to attach when talking to one engine.

    `eng` is a single entry from cfg["engines"]. `cfg` is the whole config
    dict (currently unused but reserved for a future "default api key for
    every unspec'd engine" setting). Missing / empty keys stay out of the
    returned dict so a bare install does not advertise an empty Bearer.
    """
    headers: dict[str, str] = {"User-Agent": "limcars-next/1.1"}
    key = None
    # Literal key wins when both are set, since someone who typed one
    # explicitly almost always meant that one. Documented in README.
    if eng.get("api_key"):
        key = str(eng["api_key"])
    elif eng.get("api_key_env"):
        key = os.environ.get(str(eng["api_key_env"])) or None
    if key:
        header_name = str(eng.get("auth_header") or "Authorization")
        if header_name.lower() == "authorization":
            headers[header_name] = f"Bearer {key}"
        else:
            # Non-Authorization headers carry the raw key (e.g. ComfyUI's
            # `X-API-Key: <key>`, or an nginx-proxy's custom header). No
            # "Bearer " prefix added — the user controls the final value.
            headers[header_name] = key
    extra = eng.get("headers") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and v is not None:
                # Explicit per-request override takes precedence over the
                # built-in Authorization line, so a user whose proxy wants
                # two auth headers can set both.
                headers[str(k)] = str(v)
    return headers


def make_request(url: str, *, data: bytes | None = None,
                 headers: Mapping[str, str] | None = None,
                 method: str | None = None) -> urllib.request.Request:
    """Build a urllib Request with the given headers. Thin wrapper so test
    harnesses can stub it, and so no call site forgets the User-Agent."""
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", "limcars-next/1.1")
    return urllib.request.Request(url, data=data, headers=hdrs,
                                  method=method)
