"""Admin-key queries to a LiteLLM proxy.

The OSS LiteLLM proxy's Prometheus endpoint only exposes request counts
and latency histograms — the token-level and spend metrics are gated
behind Enterprise. The admin REST surface, however, is OSS: `/spend/logs`
returns one row per request (model, user, tokens, cost) and
`/global/spend/report` returns an aggregate per date range. Both require
the master key (`LITELLM_MASTER_KEY` / `Authorization: Bearer sk-…`).

This module keeps the proxy call surface small and typed:

    spend_summary(url, master_key, days=1, timeout=5) -> {
        "window_days": int,
        "total_spend": float,
        "total_requests": int,
        "total_tokens": {"prompt": int, "completion": int},
        "by_model": [
            {"model": str, "spend": float, "requests": int,
             "prompt_tokens": int, "completion_tokens": int}, ...
        ],
        "source": "spend_logs" | "global_report",
        "error": None | str,
    }

Prefers `/global/spend/report?group_by=model` when it answers (one small
response for arbitrary ranges), falls back to `/spend/logs` (paginated,
one row per request). The master key is passed in a Bearer header and is
never written to a log, never returned to callers.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.request
from typing import Any


def _req(url: str, master_key: str, timeout: float) -> tuple[int, bytes]:
    """Small shim so tests can patch urlopen in one place."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {master_key}",
        "User-Agent": "limcars-next/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


def _try_global_report(url: str, master_key: str, days: int,
                       timeout: float) -> dict | None:
    """LiteLLM >= 1.40 exposes /global/spend/report. One call returns the
    aggregate — much cheaper than paginating /spend/logs — but earlier
    builds either 404 or 405 here, so we fall back on failure."""
    base = url.rstrip("/")
    end = _dt.datetime.now(_dt.timezone.utc).date()
    start = end - _dt.timedelta(days=days)
    q = (f"?start_date={start.isoformat()}&end_date={end.isoformat()}"
         "&group_by=model")
    try:
        status, body = _req(base + "/global/spend/report" + q, master_key,
                            timeout)
    except Exception:  # noqa: BLE001 — any transport error -> fall back
        return None
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    # Expected shape (per LiteLLM proxy source as of 1.44):
    #   [{"date": "...", "spend": float, "api_requests": int,
    #     "prompt_tokens": int, "completion_tokens": int, "model": str}, ...]
    if not isinstance(data, list):
        return None
    agg: dict[str, dict[str, Any]] = {}
    total_spend = 0.0
    total_requests = 0
    total_prompt = 0
    total_completion = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or row.get("model_group") or "unknown")
        spend = float(row.get("spend") or 0.0)
        reqs = int(row.get("api_requests") or row.get("total_requests") or 0)
        pt = int(row.get("prompt_tokens") or 0)
        ct = int(row.get("completion_tokens") or 0)
        a = agg.setdefault(model, {
            "model": model, "spend": 0.0, "requests": 0,
            "prompt_tokens": 0, "completion_tokens": 0})
        a["spend"] += spend
        a["requests"] += reqs
        a["prompt_tokens"] += pt
        a["completion_tokens"] += ct
        total_spend += spend
        total_requests += reqs
        total_prompt += pt
        total_completion += ct
    return {
        "window_days": days,
        "total_spend": round(total_spend, 6),
        "total_requests": total_requests,
        "total_tokens": {"prompt": total_prompt,
                         "completion": total_completion},
        "by_model": sorted(agg.values(), key=lambda r: -r["spend"]),
        "source": "global_report",
        "error": None,
    }


def _fallback_spend_logs(url: str, master_key: str, days: int,
                         timeout: float) -> dict:
    """Older LiteLLM: paginate /spend/logs and aggregate client-side.
    Capped at 2000 rows per call to protect this process on very busy
    proxies — the admin can lower `days` if they need a cleaner window."""
    base = url.rstrip("/")
    end = _dt.datetime.now(_dt.timezone.utc).date()
    start = end - _dt.timedelta(days=days)
    q = f"?start_date={start.isoformat()}&end_date={end.isoformat()}"
    try:
        status, body = _req(base + "/spend/logs" + q, master_key, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"window_days": days, "error": str(exc)[:200],
                "source": "spend_logs"}
    if status != 200:
        return {"window_days": days, "error": f"HTTP {status}",
                "source": "spend_logs"}
    try:
        rows = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        return {"window_days": days, "error": f"bad json: {exc}",
                "source": "spend_logs"}
    if not isinstance(rows, list):
        rows = []
    agg: dict[str, dict[str, Any]] = {}
    total_spend = 0.0
    total_prompt = 0
    total_completion = 0
    for row in rows[:2000]:
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "unknown")
        spend = float(row.get("spend") or 0.0)
        pt = int(row.get("prompt_tokens") or 0)
        ct = int(row.get("completion_tokens") or 0)
        a = agg.setdefault(model, {
            "model": model, "spend": 0.0, "requests": 0,
            "prompt_tokens": 0, "completion_tokens": 0})
        a["spend"] += spend
        a["requests"] += 1
        a["prompt_tokens"] += pt
        a["completion_tokens"] += ct
        total_spend += spend
        total_prompt += pt
        total_completion += ct
    return {
        "window_days": days,
        "total_spend": round(total_spend, 6),
        "total_requests": len(rows),
        "total_tokens": {"prompt": total_prompt,
                         "completion": total_completion},
        "by_model": sorted(agg.values(), key=lambda r: -r["spend"]),
        "source": "spend_logs",
        "truncated": len(rows) > 2000,
        "error": None,
    }


def spend_summary(url: str, master_key: str, days: int = 1,
                  timeout: float = 5.0) -> dict:
    """Return per-model spend / token / request totals for the last N days.

    Tries `/global/spend/report` first (one small response) and falls
    back to `/spend/logs` when the newer endpoint isn't available.
    Always returns a dict with an `error` field — never raises — so the
    caller can render a "LiteLLM unreachable" banner without a
    try/except.
    """
    if not url or not master_key:
        return {"window_days": days, "error": "url and master_key are required"}
    out = _try_global_report(url, master_key, days, timeout)
    if out is not None:
        return out
    return _fallback_spend_logs(url, master_key, days, timeout)
