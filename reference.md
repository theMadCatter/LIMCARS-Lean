# LIMCARS-Lean work log — security hardening + feature PRs

This file is a running record of everything I touch in this fork
(`theMadCatter/LIMCARS-Lean`), ordered chronologically. Every bullet
pairs a *finding* with the *fix* and a short rationale. New agents
picking up this task start here before anything else.

## Context on arrival

- Base commit of the owner's review: `91480f7`.
- Fork HEAD at task start: `a5f2714`. Commits added since the review
  (all upstream fixes to the llama.cpp / TandemLLM / Atlas dialects and
  the sampler):
  - `a5f2714` Merge PR #10 llama.cpp phase
  - `911b542` sampler: a dropped signal must not veto the restart baseline restore
  - `22cb38a` llamacpp: `n_decode_total` can't separate prefill from decode
  - `91480f7` Merge PR #9 llama.cpp tandem map
  - `7b4d866` dialects: complete llama.cpp + TandemLLM maps from upstream metric sources
  None of those changes touched auth, host binding, recipes, services,
  live-engine kill or the Hermes trace endpoints, so the owner's
  security findings all still apply as reported.
- Baseline test run: `/usr/bin/python3 -m pytest tests/ -q` → **66 passed**
  (README still says `44 tests` — stale, fix in PR 1).

## Findings catalogue (security review, pre-fix)

- **F-1 Default bind is LAN-wide, auth is optional** — `app/config.py`
  defaults `host: "0.0.0.0"` and `auth_token: None`; `app/server.py`
  `_authorized()` short-circuits to True when no token is set, so a
  fresh install exposes every API route to anyone on the LAN.
- **F-2 Recipe start/stop is unauthenticated** — the password gate
  (`_require_write`) only guards recipe add/update/delete.
  `/api/recipes/<id>/start` and `/stop` fall through. So the attack is
  trivial: `POST /api/recipes` with a shell command → `POST
  /api/recipes/<id>/start` → arbitrary RCE as the dashboard user.
- **F-3 Services, probe, live-engine kill are unauthenticated** — same
  class of problem. `/api/services/*/start|stop`,
  `/api/engines-live/<pid>/stop`, `/api/probe` all bypass auth entirely.
- **F-4 Localhost is always exempt** — `_is_local()` makes recipe
  changes free on `127.0.0.1`. That is friendly for a solo
  single-user box but combined with the default LAN bind it means a
  misconfigured reverse proxy upstreaming as 127.0.0.1 bypasses the
  password altogether.
- **F-5 Hermes trace password ships in URL** — the `/subagent-watch/`
  iframe is loaded with `?pw=<password>`; `_require_read_traces()`
  honours `qs['pw']`. The JS tries to `history.replaceState` it away
  but it is still logged by any reverse proxy / referrer header /
  server log on the way.
- **F-6 No login UI for the token** — `auth_token` is header-only
  (`X-Limcars-Token` / `Authorization: Bearer`). The stock HTML/JS
  never attaches it, so the moment a user sets one, their browser
  can't open the dashboard; there is no cookie login.
- **F-7 systemd unit path is wrong** — `scripts/limcars-next@.service`
  assumes `/home/%i/projects/limcars-next` which doesn't exist for a
  normal `git clone LIMCARS-Lean`. README additionally references
  `scripts/limcars.service` and `scripts/boot-test.sh` — neither
  exists in-tree.
- **F-8 Stale README** — "44 tests" vs 66 actual; security section
  claims Start/Stop stays open "for convenience" which is exactly the
  RCE path we need to close.
- **F-9 File permissions** — `data/` and the sqlite db default to
  umask (0o644); `auth.json` already dropped to 0o600 but nothing
  tightens the DB file or the `data/` dir itself.

(Nothing concerning was added in the four post-review commits — see
"Context on arrival".)

## Work plan

- **PR 1 — Secure by default.** Flip the defaults, add bearer / cookie
  auth that covers every route, require admin password for every
  mutating action behind a new `allow_exec` flag (off by default), get
  `?pw=` off the URL, tighten file perms, update tests + README +
  systemd unit.
- Feature PRs (one per topic, each with tests, each off a fresh
  branch rooted at main after PR 1 lands):
  - Per-engine auth headers / API keys (sampler, engine-info, probe).
  - LiteLLM proxy support (Prometheus + spend endpoints with master key).
  - vLLM metric compatibility for older builds (`gpu_cache_usage_perc` fallback).
  - Probe the selected engine instead of always the first.
  - Working systemd `--user` unit + install docs.
  - Status tiles for ComfyUI (`/queue`, `/system_stats`) and a generic
    HTTP health tile (e.g. Kokoro TTS).
