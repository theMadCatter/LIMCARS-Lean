"""Engine dialects: one logical schema, many Prometheus-flavoured engines.

Logical signal names consumed by the sampler and the UI:

    running            in-flight requests                gauge
    waiting            queued requests                   gauge
    kv_pct             KV/prefix pool utilisation 0-1    gauge
    prompt_tokens      cumulative prompt tokens          counter
    gen_tokens         cumulative generated tokens       counter
    prefill_tokens     tokens actively prefilled         counter (label-matched)
    prefill_seconds    cumulative prefill compute sec    counter
    decode_seconds     cumulative decode compute sec     counter
    prefix_queries     cumulative prefix-cache queries   counter
    prefix_hits        cumulative prefix-cache hits      counter
    ttft               time_to_first_token histogram     histogram (s)
    prefill_time       per-request prefill hist          histogram (s)
    decode_time        per-request decode hist           histogram (s)
    itl                inter-token latency hist          histogram (s)
    queue_time         per-request queue hist            histogram (s)
    req_success        finished requests                 counter
    draft/accepted     speculative-decode tokens         counter
    e2e                end-to-end request hist           histogram (s)

Map value forms (per logical):
    "metric_name"
    ["metric_name", "alt_name"]                 first non-null wins
    ["metric_name", {"mode": "prefill*"}]       label match; trailing * = prefix
    {"sum": [spec, spec, ...]}                   first spec required, the rest
                                                 added when present (llama.cpp
                                                 splits prompt into processed +
                                                 cached)

Metric-name sources ported from TandemDash-Fork proxy/adapters/ (vllm, sglang,
exl3, tensorfold), whose family names were verified against each upstream's
metrics_collector. A missing signal reads None ("–") in the UI, never a wrong
number.
"""
from __future__ import annotations

from . import prom


def _get(series: dict, spec, kind: str):
    """Resolve one logical entry against a parsed exposition.

    kind: 'value' -> prom.total-style sum (counters/gauges),
          'hist'  -> prom.histogram tuple.
    """
    if spec is None:
        return None
    if isinstance(spec, dict):
        parts = [_get(series, p, kind) for p in spec["sum"]]
        if parts[0] is None:
            return None
        return sum(p for p in parts if p is not None)
    if isinstance(spec, str):
        name, match, alts = spec, None, []
    else:
        name = spec[0]
        match = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else None
        alts = [x for x in spec[1:] if isinstance(x, str)]
    for nm in [name] + alts:
        if kind == "hist":
            h = prom.histogram(series, nm)
            if h is not None:
                return h
        elif match is None:
            v = prom.total(series, nm)
            if v is not None:
                return v
        else:
            # label-aware sum; trailing * in a match value = prefix match
            acc, seen = 0.0, False
            for _, labels, val in series.get(nm, ()):
                if all(_lab_ok(labels.get(k), v) for k, v in match.items()):
                    acc += val
                    seen = True
            if seen:
                return acc
    return None


def _lab_ok(actual, want) -> bool:
    if actual is None:
        return False
    if want.endswith("*"):
        return actual.startswith(want[:-1])
    return actual == want


VLLM = {
    "kind": "vllm",
    "prefix": "vllm:",
    "gauge": {
        "running": "vllm:num_requests_running",
        "waiting": "vllm:num_requests_waiting",
        # KV utilisation gauge: the current name is kv_cache_usage_perc, but
        # vLLM shipped it as `gpu_cache_usage_perc` through most of the
        # 0.4.x / 0.5.x line and renamed it only around 0.6. Volta / V100
        # rigs frequently pin an older build (flash-attn-dependent newer
        # vLLM does not run on Volta), so the alt keeps the KV cache tile
        # alive for them without a per-engine config change.
        "kv_pct": ["vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"],
    },
    "counter": {
        "prompt_tokens": "vllm:prompt_tokens_total",
        "gen_tokens": "vllm:generation_tokens_total",
        # cached_tokens is the prompt-token share served from cache. It MUST
        # stay a subset of prompt_tokens: the pricing split bills
        # (prompt - cached) at the write rate and cached at the read rate, and
        # every token TOTAL on the dash is prompt + gen. vLLM's
        # prompt_tokens_cached_total is the authoritative figure;
        # prefix_cache_hits_total is the fallback for older builds (it is
        # block-rounded, so it can sit a hair above the true cached share).
        # `gpu_prefix_cache_hits_total` is yet another name some vLLM
        # builds expose when they also expose a separate `cpu_` cache;
        # the plain name is the sum, but when it's missing we take the GPU
        # one as the closest honest proxy.
        "cached_tokens": ["vllm:prompt_tokens_cached_total",
                          "vllm:prefix_cache_hits_total",
                          "vllm:gpu_prefix_cache_hits_total",
                          "vllm:prefix_cache_hits"],
        "proc_start": "process_start_time_seconds",
        # iteration count moved from an iteration-count histogram
        # (`vllm:num_iterations_total_count`) to the current
        # `vllm:iteration_tokens_total_count` around 0.5. Both are harmless
        # here: whichever one the engine actually exposes wins.
        "iterations": ["vllm:iteration_tokens_total_count",
                       "vllm:num_iterations_total_count"],
        # Prefix-cache counters: the current names, the GPU-prefixed
        # variants a few vLLM builds briefly used when they also exposed a
        # separate `cpu_` cache, and the pre-`_total` names from the
        # earliest Prometheus-ification pass. First-match-wins, so a
        # modern install never double counts.
        "prefix_queries": ["vllm:prefix_cache_queries_total",
                           "vllm:gpu_prefix_cache_queries_total",
                           "vllm:prefix_cache_queries"],
        "prefix_hits": ["vllm:prefix_cache_hits_total",
                        "vllm:gpu_prefix_cache_hits_total",
                        "vllm:prefix_cache_hits"],
        "req_success": "vllm:request_success_total",
        # Speculative-decode counters were originally plain
        # `vllm:spec_decode_*` (0.4 era); the `_total` suffix came in with
        # the Prometheus-ification pass.
        "draft_tokens": ["vllm:spec_decode_num_draft_tokens_total",
                         "vllm:spec_decode_num_draft_tokens"],
        "accepted_tokens": ["vllm:spec_decode_num_accepted_tokens_total",
                            "vllm:spec_decode_num_accepted_tokens"],
        "drafts": ["vllm:spec_decode_num_drafts_total",
                   "vllm:spec_decode_num_drafts"],
    },
    "hist": {
        "ttft": "vllm:time_to_first_token_seconds",
        "prefill_time": "vllm:request_prefill_time_seconds",
        "decode_time": "vllm:request_decode_time_seconds",
        "itl": "vllm:inter_token_latency_seconds",
        "queue_time": "vllm:request_queue_time_seconds",
        # True end-to-end: vllm:e2e_request_latency_seconds covers the whole
        # request. request_inference_time_seconds is the RUNNING phase only and
        # silently drops queue time, so it under-reads whenever requests wait.
        "e2e": "vllm:e2e_request_latency_seconds",
    },
}

# sglang has no request_prefill_time hist and NO official prefill_throughput
# gauge; prefill speed comes from realtime_tokens_total{mode=prefill*} (which
# genuinely advances DURING prefill — the one engine with a true live signal)
# or Δprompt/Δprefill compute. Completion counting goes through the ttft
# histogram _count (verified vs sglang srt/observability/metrics_collector).
SGLANG = {
    "kind": "sglang",
    "prefix": "sglang:",
    "gauge": {
        "running": "sglang:num_running_reqs",
        "waiting": "sglang:num_queue_reqs",
        "kv_pct": "sglang:token_usage",
    },
    "counter": {
        "prompt_tokens": "sglang:prompt_tokens_total",
        "gen_tokens": "sglang:generation_tokens_total",
        "prefill_tokens": ["sglang:realtime_tokens_total", {"mode": "prefill*"}],
        "cached_tokens": "sglang:cached_tokens_total",
        "req_success": "sglang:time_to_first_token_seconds_count",
    },
    "hist": {
        "ttft": "sglang:time_to_first_token_seconds",
        "itl": "sglang:inter_token_latency_seconds",
        "e2e": "sglang:e2e_request_latency_seconds",
        "queue_time": "sglang:queue_time_seconds",
    },
}

# llama.cpp server metrics (llama-server --metrics; ggml-org/llama.cpp
# tools/server/server-task.cpp to_metrics()). Upstream facts this map relies on:
# - prompt_tokens_total counts PROCESSED prompt tokens only; cache reuse is the
#   separate prompt_tokens_cached_total. LIMCARS' prompt_tokens must include
#   cached (cached is a subset), so prompt = processed + cached.
# - tokens_predicted_total is the real name (predicted_tokens_total never
#   existed upstream; kept as an alt so nothing that read it changes) and it,
#   like the spec-decode counters, is charged when a request's slot is
#   released — so decode speed is paired with tokens_predicted_seconds_total
#   ("gen_completion_charged"), never a per-tick difference.
# - prompt_seconds_total pairs exactly with the processed count for prefill.
# - n_decode_total counts prompt batches AND generation steps, so it cannot
#   tell prefill from decode and is deliberately NOT mapped to iterations.
# - kv_cache_usage_ratio (0-1) exists on older builds only; newer ones read None.
# - no request counter and no histograms upstream: those stay None ("–").
_LCPP_PROMPT = {"sum": ["llamacpp:prompt_tokens_total", "llamacpp:prompt_tokens_cached_total"]}
LLAMACPP = {
    "kind": "llamacpp",
    "prefix": "llamacpp:",
    "gen_completion_charged": True,
    "gauge": {
        "running": "llamacpp:requests_processing",
        "waiting": "llamacpp:requests_deferred",
        "kv_pct": "llamacpp:kv_cache_usage_ratio",
    },
    "counter": {
        "prompt_tokens": _LCPP_PROMPT,
        "cached_tokens": "llamacpp:prompt_tokens_cached_total",
        "prefix_hits": "llamacpp:prompt_tokens_cached_total",
        "prefix_queries": _LCPP_PROMPT,
        "prefill_compute_tokens": "llamacpp:prompt_tokens_total",
        "prefill_seconds": "llamacpp:prompt_seconds_total",
        "gen_tokens": ["llamacpp:tokens_predicted_total", "llamacpp:predicted_tokens_total"],
        "decode_seconds": "llamacpp:tokens_predicted_seconds_total",
        "req_success": "llamacpp:requests_completed",
        "draft_tokens": "llamacpp:spec_decode_num_draft_tokens_total",
        "accepted_tokens": "llamacpp:spec_decode_num_accepted_tokens_total",
        "drafts": "llamacpp:spec_decode_num_drafts_total",
    },
    "hist": {},
}

# TandemLLM family: the engine serves its own dashboard contract; metrics are
# a small official set. qwen38: is the early-access namespace, tandemllm: the
# public one — both map identically.
# TandemLLM's native engine. Three name generations exist in the wild and all
# three must keep working, so every logical entry lists them in order (first
# non-null wins): `tandemllm:*` (the current upstream family), `qwen38:*` (the
# older family), and `qse_*` — the family the engine on this box actually
# emits, verified against its own `# HELP` text on 2026-10-24.
#
# ONE TRAP, recorded because it silently lies: the sampler's prefill truth is
#     (Δprompt_tokens − Δprefix_hits) / Δprefill_time
# and it needs prefix_hits in TOKENS (vLLM's prefix_cache_hits_total is a token
# count). `qse_cache_hits_total` counts LOOKUPS, not tokens — wiring it here
# would subtract a lookup count from a token count and inflate prefill speed.
# The token-count equivalent is `qse_prompt_tokens_cached_total` ("prompt
# tokens restored from the state store"), so THAT is prefix_hits.
# prefix_queries is qse_prompt_tokens_total (cached included): upstream
# server/METRICS.md defines the hit rate as exactly cached/prompt tokens.
# cached_tokens (savings split) is the same token count; contract 0.1.0 builds
# lack it and fall back to qse_prefill_tokens_reused_total ("prompt tokens
# restored from a snapshot"). Names verified against 0xBakeer/TandemLLM
# server/metrics.py + server/METRICS.md on 2026-10-26.
_TANDEM_MAP = {
    "gauge": {
        "running": ["tandemllm:requests_inflight", "qwen38:requests_inflight",
                    "qse_requests_running"],
        "waiting": ["tandemllm:requests_waiting", "qwen38:requests_waiting",
                    "qse_requests_waiting"],
    },
    "counter": {
        "prompt_tokens": ["tandemllm:prompt_tokens_total", "qwen38:prompt_tokens_total",
                          "qse_prompt_tokens_total"],
        "gen_tokens": ["tandemllm:completion_tokens_total", "qwen38:completion_tokens_total",
                       "qse_generation_tokens_total"],
        "req_success": ["tandemllm:requests_total", "qwen38:requests_total",
                        "qse_request_success_total"],
        # tokens genuinely run through the layers (excludes cache-restored), so
        # this is the sampler's engine-native live-prefill path
        "prefill_tokens": ["qse_prefill_tokens_forwarded_total"],
        # TOKENS restored from cache — see the trap note above
        "prefix_hits": ["qse_prompt_tokens_cached_total", "qse_prefill_tokens_reused_total"],
        "prefix_queries": ["qse_prompt_tokens_total"],
        "cached_tokens": ["qse_prompt_tokens_cached_total", "qse_prefill_tokens_reused_total"],
        "proc_start": ["qse_engine_start_time_seconds"],
        "draft_tokens": ["qse_spec_decode_num_draft_tokens_total"],
        "accepted_tokens": ["qse_spec_decode_num_accepted_tokens_total"],
        "drafts": ["qse_spec_decode_num_drafts_total"],
    },
    "hist": {
        "ttft": ["qse_time_to_first_token_seconds"],
        "itl": ["qse_time_per_output_token_seconds"],
        "e2e": ["qse_e2e_request_latency_seconds"],
        "queue_time": ["qse_request_queue_seconds"],
        "prefill_time": ["qse_request_prefill_seconds"],
    },
}
TANDEMLLM = {"kind": "tandemllm", "prefix": "tandemllm:", **_TANDEM_MAP}

# exl3 (exllamav3 worker metrics). Unique win: prefill_seconds and
# generate_seconds are COUNTERS — prefill speed = Δprompt/Δprefill_seconds
# pairs perfectly (both completion-charged counters, no histogram needed).
# decTps gauge is ABSENT when idle by design; kv_used/kv_capacity give pool
# occupancy only when a worker reports them.
EXL3 = {
    "kind": "exl3",
    "prefix": "exl3_",
    "gauge": {
        "running": "exl3_requests_running",
        "waiting": "exl3_requests_waiting",
    },
    "counter": {
        "prompt_tokens": "exl3_prompt_tokens_total",
        "gen_tokens": "exl3_generation_tokens_total",
        "req_success": "exl3_requests_total",
        "prefill_seconds": "exl3_prefill_seconds_total",
        "decode_seconds": "exl3_generate_seconds_total",
        "accepted_tokens": "exl3_accepted_draft_tokens_total",
    },
    "hist": {},
}

# TensorFold: dual-name families (mtp_* canonical, spec_decode_* alt mirrors).
TENSORFOLD = {
    "kind": "tensorfold",
    "prefix": "tensorfold:",
    "gauge": {
        "running": ["tensorfold:requests_running", "tensorfold:num_requests_running"],
        "waiting": ["tensorfold:requests_waiting", "tensorfold:num_requests_waiting"],
        "kv_pct": "tensorfold:kv_cache_usage_perc",
    },
    "counter": {
        "prompt_tokens": "tensorfold:prompt_tokens_total",
        "gen_tokens": ["tensorfold:generation_tokens_total"],
        "draft_tokens": ["tensorfold:mtp_drafted_total",
                         "tensorfold:spec_decode_num_draft_tokens_total"],
        "accepted_tokens": ["tensorfold:mtp_accepted_total",
                            "tensorfold:spec_decode_num_accepted_tokens_total"],
        "req_success": "tensorfold:requests_total",
    },
    "hist": {
        "ttft": "tensorfold:time_to_first_token_seconds",
        "e2e": "tensorfold:e2e_request_latency_seconds",
        "itl": "tensorfold:request_time_per_output_token_seconds",
        "prefill_time": ["tensorfold:request_prefill_seconds",
                         "tensorfold:request_prefill_time_seconds"],
        "decode_time": ["tensorfold:request_decode_seconds",
                        "tensorfold:request_decode_time_seconds"],
    },
}

# Atlas (github.com/Atlas-Inf/atlas, crates/spark-server/src/metrics.rs).
# Exposes no waiting/KV/e2e/itl/prefill/cached signals — those read None ("–").
# Spec decode: atlas_spec_decode_verify_total{k, outcome} counts VERIFY STEPS,
# outcome in accept|reject (K=2) or accept_all|accept_partial (dflash/mtp).
# Acceptance = steps with any accepted draft / all verify steps — the same rule
# Atlas's own TUI uses (outcome contains "accept"). It is step-level, so no
# accepted-length or drafts-per-step figure is derived from it.
ATLAS = {
    "kind": "atlas",
    "prefix": "atlas_",
    "gauge": {
        "running": "atlas_requests_active",
    },
    "counter": {
        "prompt_tokens": "atlas_prompt_tokens_total",
        "gen_tokens": "atlas_generation_tokens_total",
        "req_success": "atlas_requests_total",
        "proc_start": "process_start_time_seconds",
        # verify STEPS, not tokens: kept apart from accepted/draft_tokens so the
        # token-level spec_accept_pct stays None ("–") for Atlas
        "spec_steps_ok": ["atlas_spec_decode_verify_total", {"outcome": "accept*"}],
        "spec_steps": "atlas_spec_decode_verify_total",
    },
    "hist": {
        "ttft": "atlas_time_to_first_token_seconds",
    },
}

# LiteLLM proxy (BerriAI/litellm).
#
# The OSS LiteLLM proxy exposes a small set of Prometheus metrics; the
# "granular per-request token / spend" family is Enterprise-only. The
# dialect below covers everything a free install actually publishes:
#
#   litellm_requests_metric                     : gauge of in-flight requests
#   litellm_proxy_total_requests_metric         : counter — all requests
#   litellm_proxy_failed_requests_metric        : counter — failed subset
#   litellm_request_total_latency_metric_bucket : histogram — total latency (s)
#   litellm_llm_api_latency_metric_bucket       : histogram — upstream only
#   litellm_deployment_successful_fallbacks     : counter
#   litellm_deployment_failed_fallbacks         : counter
#
# What is NOT in OSS and therefore reads None ("–") in the dashboard:
#   * prompt_tokens / gen_tokens / cached_tokens (Enterprise `litellm_input_tokens`
#     / `litellm_output_tokens`)
#   * $/spend  (Enterprise `litellm_spend_metric`)
#   * KV cache % / prefill latency / spec decode — not relevant for a
#     routing proxy, LiteLLM does not own these numbers.
#
# For spend and token-level visibility on OSS, call the proxy's
# `/spend/logs` or `/global/spend/report` endpoints with the master key
# (see app/litellm_proxy.py and /api/litellm/spend).
LITELLM = {
    "kind": "litellm",
    "prefix": "litellm_",
    "gauge": {
        "running": "litellm_requests_metric",
    },
    "counter": {
        "req_success": "litellm_proxy_total_requests_metric",
        "req_failure": "litellm_proxy_failed_requests_metric",
    },
    "hist": {
        "e2e": "litellm_request_total_latency_metric",
        "ttft": "litellm_llm_api_latency_metric",   # closest OSS analogue
    },
}


DIALECTS = {d["kind"]: d for d in (VLLM, SGLANG, LLAMACPP, TANDEMLLM, EXL3, TENSORFOLD, ATLAS, LITELLM)}


def detect(body: str) -> str | None:
    """Identify the engine kind from a raw /metrics body."""
    probe = body[:200_000]
    if "\nvllm:" in probe or probe.startswith("# HELP vllm:"):
        return "vllm"
    if "\nsglang:" in probe:
        return "sglang"
    if "\nllamacpp:" in probe:
        return "llamacpp"
    if "\nexl3_" in probe or "\nexl3:" in probe:
        return "exl3"
    if "\ntensorfold:" in probe:
        return "tensorfold"
    if "\nqwen38:" in probe or "\ntandemllm:" in probe:
        return "tandemllm"
    # The native engine's current family (verified 2026-10-24 against its own
    # # HELP text). Deliberately checked LAST and only on two distinctive
    # family names: a body that matches any family above still resolves to it
    # exactly as before, so adding this cannot reclassify another engine.
    if "\nqse_requests_running" in probe or "\nqse_generation_tokens_total" in probe:
        return "tandemllm"
    # Atlas: checked after every existing family, on its own two core names only
    if ("\natlas_requests_" in probe or "\natlas_generation_tokens_total" in probe
            or probe.startswith("# HELP atlas_")):
        return "atlas"
    # LiteLLM proxy — detected last on its own stable OSS metric names so a
    # body that also mentions `litellm_*` labels inside another engine's
    # help text (unlikely but possible) still resolves to that engine.
    if ("\nlitellm_proxy_total_requests_metric" in probe
            or "\nlitellm_requests_metric" in probe
            or probe.startswith("# HELP litellm_")):
        return "litellm"
    return None


def read_gauges(series: dict, dial: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for logical, spec in dial.get("gauge", {}).items():
        out[logical] = _get(series, spec, "value")
    return out


def read_counters(series: dict, dial: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for logical, spec in dial.get("counter", {}).items():
        out[logical] = _get(series, spec, "value")
    return out


def read_hists(series: dict, dial: dict) -> dict[str, tuple | None]:
    out: dict[str, tuple | None] = {}
    for logical, spec in dial.get("hist", {}).items():
        out[logical] = _get(series, spec, "hist")
    return out
