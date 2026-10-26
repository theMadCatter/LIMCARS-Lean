"""LiteLLM proxy dialect + /spend/{report,logs} aggregation.

Covered:
  * `dialects.detect()` identifies a LiteLLM metrics body.
  * The dialect pulls out the OSS metric names (requests gauge + total
    request counter + total latency histogram) and leaves every
    enterprise-only logical field (prompt_tokens, gen_tokens, cached,
    spec, …) as None rather than fabricating zeros.
  * `litellm_proxy.spend_summary()` prefers `/global/spend/report`,
    falls back to `/spend/logs` on 404, aggregates both into the same
    shape, and NEVER leaks the master key to the caller.
"""
from __future__ import annotations

import json

from app import dialects, prom
from app import litellm_proxy as llm


LITELLM_BODY = """# HELP litellm_requests_metric In-flight requests
# TYPE litellm_requests_metric gauge
litellm_requests_metric{model="gpt-4o"} 3.0
# HELP litellm_proxy_total_requests_metric Total requests
# TYPE litellm_proxy_total_requests_metric counter
litellm_proxy_total_requests_metric{model="gpt-4o"} 1234.0
litellm_proxy_total_requests_metric{model="claude-3"} 500.0
# HELP litellm_proxy_failed_requests_metric Failed requests
# TYPE litellm_proxy_failed_requests_metric counter
litellm_proxy_failed_requests_metric{model="gpt-4o"} 12.0
# HELP litellm_request_total_latency_metric Total request latency
# TYPE litellm_request_total_latency_metric histogram
litellm_request_total_latency_metric_bucket{le="0.5"} 100.0
litellm_request_total_latency_metric_bucket{le="1.0"} 500.0
litellm_request_total_latency_metric_bucket{le="2.0"} 1500.0
litellm_request_total_latency_metric_bucket{le="+Inf"} 1734.0
litellm_request_total_latency_metric_sum 1800.0
litellm_request_total_latency_metric_count 1734.0
"""


class TestDialect:
    def test_detect(self):
        assert dialects.detect(LITELLM_BODY) == "litellm"

    def test_counters_and_gauges(self):
        s = prom.parse(LITELLM_BODY)
        gauges = dialects.read_gauges(s, dialects.LITELLM)
        counters = dialects.read_counters(s, dialects.LITELLM)
        # Multiple labelsets for the gauge sum (per prom.total()).
        assert gauges["running"] == 3.0
        assert counters["req_success"] == 1234.0 + 500.0
        assert counters["req_failure"] == 12.0

    def test_enterprise_only_fields_are_none(self):
        """Prompt / gen / cached tokens must not fabricate zeros — the
        LiteLLM OSS Prometheus endpoint does NOT publish them, and the
        dashboard's honesty rule is '–' never a fake number."""
        s = prom.parse(LITELLM_BODY)
        counters = dialects.read_counters(s, dialects.LITELLM)
        for k in ("prompt_tokens", "gen_tokens", "cached_tokens"):
            assert counters.get(k) is None

    def test_latency_histogram_parsed(self):
        s = prom.parse(LITELLM_BODY)
        hists = dialects.read_hists(s, dialects.LITELLM)
        e2e = hists["e2e"]
        assert e2e is not None
        buckets, s_, c_ = e2e
        assert buckets[-1][0] == float("inf")
        assert c_ == 1734.0


class TestSpendSummary:
    def test_prefers_global_report(self, monkeypatch):
        """When /global/spend/report answers 200, /spend/logs is never
        called (saves pagination traffic on busy proxies)."""
        calls = []

        def fake_req(url, key, timeout):
            calls.append(url)
            if "/global/spend/report" in url:
                body = json.dumps([
                    {"model": "gpt-4o", "spend": 1.25,
                     "api_requests": 100,
                     "prompt_tokens": 1000, "completion_tokens": 500},
                    {"model": "gpt-4o", "spend": 0.5,
                     "api_requests": 20,
                     "prompt_tokens": 300, "completion_tokens": 100},
                    {"model": "claude-3", "spend": 0.1,
                     "api_requests": 5,
                     "prompt_tokens": 100, "completion_tokens": 50},
                ]).encode()
                return 200, body
            return 404, b""

        monkeypatch.setattr(llm, "_req", fake_req)
        out = llm.spend_summary("http://litellm.local:4000", "sk-master",
                                days=7)
        assert out["source"] == "global_report"
        assert out["total_requests"] == 125
        assert round(out["total_spend"], 2) == 1.85
        assert out["total_tokens"]["prompt"] == 1400
        assert out["total_tokens"]["completion"] == 650
        # gpt-4o is aggregated across the two report rows.
        gpt = [r for r in out["by_model"] if r["model"] == "gpt-4o"][0]
        assert gpt["requests"] == 120
        assert round(gpt["spend"], 2) == 1.75
        # Ordering: highest spend first.
        assert out["by_model"][0]["model"] == "gpt-4o"
        # Confirm /spend/logs was NOT called when the newer endpoint
        # answered.
        assert all("/spend/logs" not in c for c in calls)

    def test_falls_back_to_spend_logs(self, monkeypatch):
        def fake_req(url, key, timeout):
            if "/global/spend/report" in url:
                return 404, b""
            if "/spend/logs" in url:
                body = json.dumps([
                    {"model": "gpt-4o", "spend": 0.5,
                     "prompt_tokens": 100, "completion_tokens": 50},
                    {"model": "gpt-4o", "spend": 0.2,
                     "prompt_tokens": 50,  "completion_tokens": 10},
                    {"model": "mistral-7b", "spend": 0.001,
                     "prompt_tokens": 10,  "completion_tokens": 5},
                ]).encode()
                return 200, body
            return 500, b""

        monkeypatch.setattr(llm, "_req", fake_req)
        out = llm.spend_summary("http://litellm.local:4000", "sk-master",
                                days=1)
        assert out["source"] == "spend_logs"
        assert out["total_requests"] == 3
        assert round(out["total_spend"], 3) == 0.701

    def test_missing_master_key_returns_error_not_raise(self):
        out = llm.spend_summary("http://litellm.local:4000", "", days=1)
        assert out["error"]
        assert "master_key" in out["error"]

    def test_http_error_returns_error_not_raise(self, monkeypatch):
        monkeypatch.setattr(llm, "_req", lambda *a, **k: (401, b"nope"))
        out = llm.spend_summary("http://x.local", "sk", days=1)
        assert out["error"] == "HTTP 401"
        assert out["source"] == "spend_logs"
