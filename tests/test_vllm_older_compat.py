"""vLLM metric-name compatibility for older builds.

Volta-class GPUs (V100) are frequently stuck on older vLLM releases
(0.4.x / 0.5.x) because newer builds depend on flash-attn kernels that
Volta cannot run. The owner's `defiantic` box fits that profile. Older
vLLM used slightly different Prometheus metric names, and silently
losing the KV-cache tile / spec-decode table because of a rename is
exactly the kind of regression this test file is here to catch.

Covered:
  * `vllm:gpu_cache_usage_perc` is still recognised as `kv_pct` even
    when the newer `vllm:kv_cache_usage_perc` is absent.
  * Spec-decode counters without the `_total` suffix still populate the
    draft / accepted / drafts logical fields (used by both the dialect
    and the engine-info panel).
  * `vllm:cache_config` (pre-rename) is used as a fallback by
    `engine_info.info()` when `vllm:cache_config_info` is missing.
"""
from __future__ import annotations

from app import dialects, prom
from app.engine_info import info as _info_fn  # imported for monkeypatching urlopen below


OLDER_VLLM_BODY = """# HELP vllm:num_requests_running Running
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="m"} 1.0
vllm:num_requests_waiting{model_name="m"} 0.0
# older gauge name
vllm:gpu_cache_usage_perc{model_name="m"} 0.33
# counters (older spec-decode names, no _total)
vllm:prompt_tokens_total{model_name="m"} 1000.0
vllm:generation_tokens_total{model_name="m"} 500.0
vllm:spec_decode_num_drafts{model_name="m"} 20.0
vllm:spec_decode_num_draft_tokens{model_name="m"} 60.0
vllm:spec_decode_num_accepted_tokens{model_name="m"} 44.0
# pre-rename config info metric
vllm:cache_config{num_gpu_blocks="2000",block_size="16",kv_cache_size_tokens="32000",max_model_len="4096",cache_dtype="auto"} 1.0
"""


def test_detect_still_says_vllm():
    assert dialects.detect(OLDER_VLLM_BODY) == "vllm"


def test_kv_pct_falls_back_to_gpu_cache_usage_perc():
    series = prom.parse(OLDER_VLLM_BODY)
    gauges = dialects.read_gauges(series, dialects.VLLM)
    assert gauges["kv_pct"] == 0.33


def test_spec_decode_counters_without_total_suffix():
    series = prom.parse(OLDER_VLLM_BODY)
    counters = dialects.read_counters(series, dialects.VLLM)
    assert counters["drafts"] == 20.0
    assert counters["draft_tokens"] == 60.0
    assert counters["accepted_tokens"] == 44.0


def test_newer_name_still_preferred_when_both_exist():
    """A build that transiently exposed both names must not double-count."""
    body = OLDER_VLLM_BODY + 'vllm:kv_cache_usage_perc{model_name="m"} 0.55\n'
    series = prom.parse(body)
    gauges = dialects.read_gauges(series, dialects.VLLM)
    # The current name wins (first-listed in the alt list).
    assert gauges["kv_pct"] == 0.55


def test_info_panel_reads_cache_config_fallback(monkeypatch):
    """engine_info.info() uses vllm:cache_config when the renamed
    vllm:cache_config_info is missing (pre-0.5 vLLM)."""
    import urllib.request as _urlreq

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url_or_req, timeout=None):
        url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
        if url.endswith("/metrics"):
            return FakeResp(OLDER_VLLM_BODY.encode())
        if url.endswith("/v1/models"):
            return FakeResp(b'{"data":[{"id":"llama-7b"}]}')
        if url.endswith("/version"):
            return FakeResp(b'{"version":"0.4.2"}')
        return FakeResp(b"")

    monkeypatch.setattr(_urlreq, "urlopen", fake_urlopen)
    out = _info_fn("http://127.0.0.1:9999")
    assert out["healthy"] is True
    # Fallback label parsing picked up the config:
    assert out["pool_tokens"] == 32000
    assert out["block_size"] == 16
    assert out["num_gpu_blocks"] == 2000
    assert out["kv_dtype"] == "auto"
    # Draft-K from the older spec-decode counters (dtok / drafts = 60/20)
    assert out["draft_k"] == 3.0
    # 44 accepted / 60 drafted = 73.3% acceptance.
    assert out["accept_rate"] == 73.3
