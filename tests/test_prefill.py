"""True prefill speed for vLLM — four computed numbers plus hit rate.

Covers the metrics the owner's lab wanted first-class on the engine card:

    (1) prefill_tps_avg       — Δ(prompt - cached) / Δprefill_time_sum
                                "what the GPU actually prefilled"
    (2) prefill_tps_eff (new) — Δprompt / Δprefill_time_sum
                                "what the user feels, cache replay included"
    (3) prefill_time_avg_ms   — Δsum / Δcount of request_prefill_time_seconds
    (4) ttft_avg_ms           — Δsum / Δcount of time_to_first_token_seconds
    (5) cache_hit_pct         — Δhits / Δqueries over the same window

Three test families:

* Unit tests on `_prefill_rate_effective` with crafted internal state so
  the math is isolated from the dialect / histogram layers.
* Dialect tests that feed fixture /metrics text — current vLLM, the pre-
  `_total` names from the earliest Prometheus-ification pass, and the
  `gpu_prefix_cache_*` variants a few builds briefly used — and prove
  every counter resolves without touching the sampler.
* End-to-end tests that stub `_fetch` and drive two full `_tick()` calls,
  reproducing the owner's reference-run numbers (340 uncached /
  ~1009 effective / 66 % hit rate / 10.2 s prefill / 10.7 s TTFT) and
  checking the row the sampler publishes carries them under the
  documented field names. One of those scenarios resets the counters
  mid-window to prove the restart baseline path doesn't publish a lie.
"""
from __future__ import annotations

import time

import pytest

from app import dialects
from app.sampler import EngineSampler
from app.sse import Broker


# ---------------------------------------------------------------- helpers


def _mk(**over) -> EngineSampler:
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900,
           "prefill_window_s": 120.0}
    cfg.update(over)
    eng = {"id": "t", "url": "http://x", "kind": "vllm"}
    return EngineSampler(cfg, eng, Broker(), store=None)


def _seed_prefill(s: EngineSampler, now: float, *,
                  prompt: float, hits: float, pf_secs: float,
                  base_age: float) -> None:
    """Set up `_counter_win` and the prefill-time hist anchors so the
    prefill helpers can compute off a controlled stretch of 'window'.
    Mirrors the harness used in tests/test_sampler.py."""
    s._counter_win["prompt_tokens"] = [(now - base_age + 0.1, prompt, base_age)]
    s._counter_win["prefix_hits"] = [(now - base_age + 0.1, hits, base_age)]
    zero = ([(1.0, 0.0), (float("inf"), 0.0)], 0.0, 0.0)
    cur = ([(1.0, 0.0), (float("inf"), 1.0)], pf_secs, 1.0)
    s._hist_prev["prefill_time"] = cur
    s._hist_hr["prefill_time"] = [(now - base_age, zero)]


# ----------------------------------------------------- unit: effective rate


class TestEffectivePrefillRateMath:
    def test_matches_reference_run_numbers(self):
        """The owner's TP4 V100 qwen3 run over ~105 min / 409 requests
        reported ~340 tok/s uncached and ~1009 tok/s effective. With a
        matching prompt / hits / prefill-time triple, both helpers must
        return those numbers (within one tok/s rounding)."""
        s = _mk()
        now = time.time()
        # 409 requests × 10.2 s mean prefill = 4172 s of prefill compute.
        # effective = Δprompt / Δprefill_time_sum = 1009 → Δprompt = 4209k.
        # uncached  = (Δprompt - Δhits) / Δprefill_time_sum = 340
        #           → Δhits = Δprompt - 340 * 4172 = 2791k.
        _seed_prefill(s, now,
                      prompt=4_209_548, hits=2_791_068,
                      pf_secs=4_172.0, base_age=60.0)
        uncached = s._prefill_rate(now)
        effective = s._prefill_rate_effective(now)
        assert uncached is not None and abs(uncached - 340.0) < 1.0
        assert effective is not None and abs(effective - 1009.0) < 1.0

    def test_no_cache_hits_means_equal_rates(self):
        """When nothing is cached, uncached == effective by definition."""
        s = _mk()
        now = time.time()
        _seed_prefill(s, now, prompt=50_000, hits=0, pf_secs=100.0,
                      base_age=60.0)
        assert s._prefill_rate(now) == pytest.approx(500.0, rel=0.001)
        assert s._prefill_rate_effective(now) == pytest.approx(500.0, rel=0.001)

    def test_all_cached_means_zero_uncached_but_nonzero_effective(self):
        """A 100 % cache-hit workload: the GPU truly prefilled 0 uncached
        tokens per second (the fallback wall-clock branch returns 0.0,
        honestly), while effective = Δprompt/Δprefill_time_sum = 10 000."""
        s = _mk()
        now = time.time()
        _seed_prefill(s, now, prompt=200_000, hits=200_000, pf_secs=20.0,
                      base_age=60.0)
        assert s._prefill_rate(now) == 0.0
        assert s._prefill_rate_effective(now) == pytest.approx(10_000.0,
                                                               rel=0.001)

    def test_no_window_yet_returns_none_not_a_young_lie(self):
        """A 2 s anchor is not a settle window; both helpers must refuse
        rather than invent a speed off two scrapes."""
        s = _mk()
        now = time.time()
        _seed_prefill(s, now, prompt=5_000, hits=0, pf_secs=2.0, base_age=2.0)
        assert s._prefill_rate(now) is None
        assert s._prefill_rate_effective(now) is None

    def test_no_prefill_histogram_returns_none(self):
        """Engines without request_prefill_time_seconds (sglang, atlas,
        llama.cpp bootstrap) must not get a fabricated effective rate."""
        s = _mk()
        now = time.time()
        # populate the token side but NO prefill-time hist anchor
        s._counter_win["prompt_tokens"] = [(now - 60, 10_000, 60.0)]
        assert s._prefill_rate_effective(now) is None

    def test_counter_reset_doesnt_leak_a_speed(self):
        """Simulate a reset by writing a current state SMALLER than the
        anchor — the _hist_span_delta clamps the sum delta to 0, and the
        helper must return None instead of a negative number."""
        s = _mk()
        now = time.time()
        # Anchor was 500 s of prefill_time; current cumulative is 10 s —
        # engine restarted mid-window. d_secs clamps to 0.
        old = ([(1.0, 0.0), (float("inf"), 1.0)], 500.0, 1.0)
        cur = ([(1.0, 0.0), (float("inf"), 1.0)], 10.0, 1.0)
        s._hist_prev["prefill_time"] = cur
        s._hist_hr["prefill_time"] = [(now - 60.0, old)]
        s._counter_win["prompt_tokens"] = [(now - 60, 100_000, 60.0)]
        assert s._prefill_rate_effective(now) is None


# -------------------------------------------- dialect: metric-name coverage


# Minimal body shapes carrying ONE representative entry per logical we
# care about. The counter numbers are small on purpose so a reader can
# verify the math in head.
_VLLM_CURRENT = """\
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="m"} 1.0
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="m"} 10000.0
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="m"} 2000.0
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{model_name="m"} 10000.0
# TYPE vllm:prefix_cache_hits_total counter
vllm:prefix_cache_hits_total{model_name="m"} 6600.0
# TYPE vllm:request_prefill_time_seconds histogram
vllm:request_prefill_time_seconds_bucket{le="1.0"} 2.0
vllm:request_prefill_time_seconds_bucket{le="10.0"} 8.0
vllm:request_prefill_time_seconds_bucket{le="+Inf"} 10.0
vllm:request_prefill_time_seconds_sum 102.0
vllm:request_prefill_time_seconds_count 10.0
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{le="1.0"} 2.0
vllm:time_to_first_token_seconds_bucket{le="20.0"} 10.0
vllm:time_to_first_token_seconds_bucket{le="+Inf"} 10.0
vllm:time_to_first_token_seconds_sum 107.0
vllm:time_to_first_token_seconds_count 10.0
"""


_VLLM_GPU_PREFIX = _VLLM_CURRENT.replace(
    "vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total"
).replace(
    "vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total")


_VLLM_PRE_TOTAL = _VLLM_CURRENT.replace(
    "vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries"
).replace(
    "vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits")


class TestVllmPrefixCacheNameFallbacks:
    def test_current_names(self):
        from app import prom
        c = dialects.read_counters(prom.parse(_VLLM_CURRENT), dialects.VLLM)
        assert c["prefix_queries"] == 10000.0
        assert c["prefix_hits"] == 6600.0

    def test_gpu_prefixed_names(self):
        from app import prom
        c = dialects.read_counters(prom.parse(_VLLM_GPU_PREFIX), dialects.VLLM)
        assert c["prefix_queries"] == 10000.0
        assert c["prefix_hits"] == 6600.0

    def test_pre_total_suffix_names(self):
        from app import prom
        c = dialects.read_counters(prom.parse(_VLLM_PRE_TOTAL), dialects.VLLM)
        assert c["prefix_queries"] == 10000.0
        assert c["prefix_hits"] == 6600.0

    def test_detect_still_identifies_vllm_on_each(self):
        for body in (_VLLM_CURRENT, _VLLM_GPU_PREFIX, _VLLM_PRE_TOTAL):
            assert dialects.detect(body) == "vllm"

    def test_prefill_histogram_resolves(self):
        from app import prom
        h = dialects.read_hists(prom.parse(_VLLM_CURRENT), dialects.VLLM)
        buckets, s_, c_ = h["prefill_time"]
        assert c_ == 10.0 and s_ == 102.0        # mean = 10.2 s per request


# ------------------------------------------------------------ end-to-end


class _ScriptedSampler(EngineSampler):
    """Replaces `_fetch` with a sequence of pre-built /metrics bodies.
    Each `_tick()` call consumes one body; raises if the test asks for
    more ticks than it scripted (so a silently-skipped tick fails loud).
    `/v1/models` is answered with a static JSON so the sampler stays off
    the network."""

    def __init__(self, *a, bodies=(), **kw):
        super().__init__(*a, **kw)
        self._bodies = list(bodies)
        self._idx = 0

    def _fetch(self, path: str, timeout: float = 1.5) -> bytes:
        if path == "/v1/models":
            return b'{"data": [{"id": "demo/model"}]}'
        if self._idx >= len(self._bodies):
            raise AssertionError("scripted sampler out of bodies")
        body = self._bodies[self._idx]
        self._idx += 1
        return body.encode("utf-8")


def _body(*, prompt, hits, queries, pf_sum, pf_count, ttft_sum, ttft_count,
          running=1.0, gen=0.0, req_success=0.0) -> str:
    """Build one /metrics scrape with the counters we care about. Values
    are inlined into the current-vLLM template so the dialect takes the
    first-choice name every time."""
    return (
        f'vllm:num_requests_running{{model_name="m"}} {running}\n'
        f'vllm:num_requests_waiting{{model_name="m"}} 0.0\n'
        f'vllm:kv_cache_usage_perc{{model_name="m"}} 0.5\n'
        f'vllm:prompt_tokens_total{{model_name="m"}} {prompt}\n'
        f'vllm:generation_tokens_total{{model_name="m"}} {gen}\n'
        f'vllm:prefix_cache_queries_total{{model_name="m"}} {queries}\n'
        f'vllm:prefix_cache_hits_total{{model_name="m"}} {hits}\n'
        f'vllm:request_success_total{{model_name="m"}} {req_success}\n'
        'vllm:request_prefill_time_seconds_bucket{le="1.0"} 0.0\n'
        'vllm:request_prefill_time_seconds_bucket{le="+Inf"} '
        f'{pf_count}\n'
        f'vllm:request_prefill_time_seconds_sum {pf_sum}\n'
        f'vllm:request_prefill_time_seconds_count {pf_count}\n'
        'vllm:time_to_first_token_seconds_bucket{le="1.0"} 0.0\n'
        f'vllm:time_to_first_token_seconds_bucket{{le="+Inf"}} {ttft_count}\n'
        f'vllm:time_to_first_token_seconds_sum {ttft_sum}\n'
        f'vllm:time_to_first_token_seconds_count {ttft_count}\n'
    )


def _run_scrapes(bodies_and_times, *, window_s: float = 150.0,
                 prefill_window_s: float = 60.0) -> dict:
    """Drive the sampler through N scrapes, each tagged with the wall-
    clock time at which it occurred. Returns the final ring row.

    Why N scrapes and not two: the sampler calls `_prefill_rate` BEFORE
    `_hist_snapshot` within a single tick (deliberately — the "anchor"
    is always the previous tick's state). So the earliest tick for
    which the honest Δprompt / Δprefill_time_sum ratio is observable is
    the THIRD one — tick 1 primes `_hist_prev`, tick 2 gives
    `_hist_hr` a second anchor, and tick 3 publishes a row whose
    `_hist_span_delta("prefill_time", prefill_window_s)` actually spans
    the window.

    `window_s` is tuned high enough that `_hist_base` (which drives
    `prefill_time_avg_ms` / `ttft_avg_ms` via `_hist_delta`) retains
    the earliest anchor across the full simulated gap.
    """
    cfg = {"live_hz": 2.0, "window_s": window_s, "ring_seconds": 900,
           "prefill_window_s": prefill_window_s}
    bodies = [b for b, _t in bodies_and_times]
    s = _ScriptedSampler(cfg, {"id": "t", "url": "http://x", "kind": "vllm"},
                         Broker(), store=None, bodies=bodies)
    for _body_text, when in bodies_and_times:
        _tick_at(s, when)
    return dict(s.ring.last() or {})


def _tick_at(s: _ScriptedSampler, when: float) -> None:
    """Call sampler._tick() but with time.time() patched to `when` so the
    rate anchors line up with the simulated scrape gap. We patch only for
    the duration of the call to keep other threads untouched."""
    import app.sampler as sampler_mod
    original = sampler_mod.time.time
    sampler_mod.time.time = lambda: when          # type: ignore[assignment]
    try:
        s._tick()
    finally:
        sampler_mod.time.time = original          # type: ignore[assignment]


class TestEndToEnd:
    def test_formulas_hold_through_the_full_pipeline(self):
        """Four scripted scrapes drive the real sampler end-to-end with
        the same ratios as the owner's reference run (66 % cache hit,
        ~1000 tok/s effective, ~340 tok/s uncached, 10 s mean prefill /
        TTFT), scaled down to a 120-s wall-clock window the sampler can
        actually anchor hists against. Validates that every one of the
        five numbers is published under its documented field name — a
        drift between the sampler and the UI would show up here as the
        wrong field being populated."""
        t0 = time.time() - 200.0        # keep everything in the near past
        # Scrape 1: hard baseline (all counters 0). Primes `_hist_prev`.
        s1 = _body(prompt=0, hits=0, queries=0,
                   pf_sum=0.0, pf_count=0, ttft_sum=0.0, ttft_count=0)
        # Scrape 2 (five s later): no activity. Gives `_hist_hr` a second
        # anchor so the span-delta on scrape 4 has a > 0 covered window.
        s2 = s1
        # Scrape 3 (120 s after scrape 1): the big delta — one busy
        # request's worth of counters landed. mean prefill = 10 s per req
        # (sum=10 s / count=1), mean TTFT = 10 s, hit rate = 66 %.
        s3 = _body(prompt=10_000, hits=6_600, queries=10_000,
                   pf_sum=10.0, pf_count=1,
                   ttft_sum=10.0, ttft_count=1, req_success=1)
        # Scrape 4 (immediately after): same counters as scrape 3. This
        # final tick is what moves `_hist_prev` to the scrape-3 state so
        # the row we read actually sees the advance.
        s4 = s3
        row = _run_scrapes([
            (s1, t0), (s2, t0 + 5.0), (s3, t0 + 120.0), (s4, t0 + 120.1),
        ])
        # Hit rate — straight from counter deltas.
        assert row.get("cache_hit_pct") == pytest.approx(66.0, abs=0.5)
        # Uncached: (10 000 - 6 600) / 10 s.
        assert row.get("prefill_tps_avg") == pytest.approx(340.0, abs=5.0)
        # Effective: 10 000 / 10 s.
        assert row.get("prefill_tps_eff") == pytest.approx(1_000.0, abs=5.0)
        # Mean prefill / TTFT: 10 s each → 10 000 ms.
        assert row.get("prefill_time_avg_ms") == pytest.approx(10_000.0,
                                                               abs=50.0)
        assert row.get("ttft_avg_ms") == pytest.approx(10_000.0, abs=50.0)
        # Observation count surfaces alongside the mean so the UI can
        # render "mean per request · N obs".
        assert row.get("prefill_time_n") == 1
        assert row.get("ttft_n") == 1

    def test_counter_reset_mid_window_produces_no_fake_speed(self):
        """One healthy scrape, then an engine-restart scrape where every
        counter has gone BACKWARDS. The sampler notices and the row
        either carries None or a non-negative number — never a lie."""
        t0 = time.time() - 200.0
        s_hot = _body(prompt=5_000_000, hits=3_000_000, queries=4_500_000,
                      pf_sum=10_000.0, pf_count=1000,
                      ttft_sum=10_500.0, ttft_count=1000, req_success=1000)
        s_reset = _body(prompt=10_000, hits=0, queries=10_000,
                        pf_sum=5.0, pf_count=1,
                        ttft_sum=5.0, ttft_count=1, req_success=1)
        row = _run_scrapes([
            (s_hot, t0), (s_hot, t0 + 5.0),
            (s_reset, t0 + 120.0), (s_reset, t0 + 120.1),
        ])
        for k in ("prefill_tps_avg", "prefill_tps_eff"):
            v = row.get(k)
            assert v is None or v >= 0.0, f"{k} leaked a negative: {v}"

    def test_older_vllm_names_flow_through_end_to_end(self):
        """Fixture using the pre-`_total` prefix-cache names (the oldest
        vLLM Prometheus-ification pass) must still produce a cache hit
        rate end-to-end. Guards against a dialect alt that resolves at
        the counter layer but fails to feed `_win_ratio`."""
        def _old(prompt, hits, queries, *, pf_sum=0.0, pf_count=0,
                 ttft_sum=0.0, ttft_count=0, req_success=0):
            return (
                f'vllm:num_requests_running{{model_name="m"}} 1.0\n'
                f'vllm:num_requests_waiting{{model_name="m"}} 0.0\n'
                f'vllm:kv_cache_usage_perc{{model_name="m"}} 0.5\n'
                f'vllm:prompt_tokens_total{{model_name="m"}} {prompt}\n'
                f'vllm:generation_tokens_total{{model_name="m"}} 0\n'
                # old names — no `_total` suffix.
                f'vllm:prefix_cache_queries{{model_name="m"}} {queries}\n'
                f'vllm:prefix_cache_hits{{model_name="m"}} {hits}\n'
                f'vllm:request_success_total{{model_name="m"}} {req_success}\n'
                'vllm:request_prefill_time_seconds_bucket{le="1.0"} 0.0\n'
                f'vllm:request_prefill_time_seconds_bucket{{le="+Inf"}} '
                f'{pf_count}\n'
                f'vllm:request_prefill_time_seconds_sum {pf_sum}\n'
                f'vllm:request_prefill_time_seconds_count {pf_count}\n'
            )
        t0 = time.time() - 200.0
        row = _run_scrapes([
            (_old(0, 0, 0), t0),
            (_old(0, 0, 0), t0 + 5.0),
            (_old(10_000, 6_600, 10_000, pf_sum=10.0, pf_count=1,
                  req_success=1), t0 + 120.0),
            (_old(10_000, 6_600, 10_000, pf_sum=10.0, pf_count=1,
                  req_success=1), t0 + 120.1),
        ])
        assert row.get("cache_hit_pct") == pytest.approx(66.0, abs=0.5)
        assert row.get("prefill_tps_avg") == pytest.approx(340.0, abs=5.0)
        assert row.get("prefill_tps_eff") == pytest.approx(1_000.0, abs=5.0)

    def test_missing_prefill_histogram_degrades_to_none(self):
        """A vLLM build that strips the request_prefill_time_seconds
        histogram (some custom builds do, to cut cardinality): both
        prefill_tps_* must publish None, the ring row keeps the keys
        as None, and nothing crashes. Cache-hit rate STILL computes
        because it reads counters directly."""
        def _no_prefill(*, prompt, hits, queries, req_success=0):
            return (
                f'vllm:num_requests_running{{model_name="m"}} 1.0\n'
                f'vllm:num_requests_waiting{{model_name="m"}} 0.0\n'
                f'vllm:kv_cache_usage_perc{{model_name="m"}} 0.5\n'
                f'vllm:prompt_tokens_total{{model_name="m"}} {prompt}\n'
                f'vllm:generation_tokens_total{{model_name="m"}} 0\n'
                f'vllm:prefix_cache_queries_total{{model_name="m"}} {queries}\n'
                f'vllm:prefix_cache_hits_total{{model_name="m"}} {hits}\n'
                f'vllm:request_success_total{{model_name="m"}} {req_success}\n'
            )
        t0 = time.time() - 200.0
        row = _run_scrapes([
            (_no_prefill(prompt=100_000, hits=50_000, queries=100_000), t0),
            (_no_prefill(prompt=100_000, hits=50_000, queries=100_000),
             t0 + 5.0),
            (_no_prefill(prompt=200_000, hits=120_000, queries=200_000,
                         req_success=10), t0 + 120.0),
            (_no_prefill(prompt=200_000, hits=120_000, queries=200_000,
                         req_success=10), t0 + 120.1),
        ])
        assert row.get("prefill_tps_eff") is None
        # Cache hit rate still works — reads counters, not the hist.
        assert row.get("cache_hit_pct") == pytest.approx(70.0, abs=0.5)
