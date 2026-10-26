# Changelog

## LIMCARS-Lean 0.2.0 — UX pass

Visual design unchanged; additions only. No honesty-invariant change.

- **Engine switcher**: the header engine pill opens a list of every configured
  engine (shows ▾ only with 2+). Selection persists in the `limcars_ui` cookie and
  `?engine=`; bubbles, charts and the pricing note follow it. `/api/snapshot`
  accepts `engine=` to resolve that engine's pricing.
- **Tab status**: title shows live decode speed + model; the favicon gets a
  yellow (stale) / red (down) dot only when something is wrong.
- **Toasts + in-page confirm** replace every native `alert()` / `confirm()`.
- **Stale-data banner**: when the stream drops, the engine goes down, or no row
  arrives for 10 s, a banner says how old the values are and live bubbles dim.
- **Bubble tooltips** explain each figure and its rule (p50 only at n≥20,
  cached ⊂ prompt, "at this rate" projections, synthetic minutes excluded).
- **Phone layout**: display controls fold behind one ⚙️ button below 860 px;
  wide tables scroll inside their card instead of widening the page.
- **Pricing editor**: inline validation, Enter saves / Esc closes, saved toast.
  Fixes two pre-existing bugs: blank write+out ("clear override") was blocked by
  the old check, and the server rejected every save because the editor's
  `display_name`/`provider` strings failed the numbers-only validation.
- **Accessibility**: aria labels/states on icon buttons, popovers and window
  pills; visible `:focus-visible` rings; arrow-key navigation in palette and
  engine popovers; Esc returns focus.
- **OS preferences**: theme follows `prefers-color-scheme` until the user picks
  one; `prefers-reduced-motion` disables animations.
- **Collapsible sidebar sections** (Panel settings, Live engines, Security,
  Recipes), remembered per browser.
- Cache stamps → `?v=20261026b`. `node --check` passes; `pytest` 44 passed.

## LIMCARS-Lean 0.1.0 — derived from LIMCARS @ `7c9098e`

Optimization/cleanup pass only. No feature, API-shape, visual or honesty-invariant
change (`counter_buckets.ts` minute alignment, `synthetic=1` exclusion, prefill
cap `PREFILL_MAX = 12000`, counter-reset handling, cached ⊂ prompt savings math,
n>=20 percentile rule are untouched). `data/` was not imported.

### Backend (`app/`)
- **sampler.py**
  - Histogram/series key names are built once at import (`_HIST_KEYS`,
    `SERIES_KEYS`) instead of f-string-formatting ~40 keys on every 2 Hz tick.
  - The e2e 1 h histogram slice was computed twice per tick; the duplicate is gone.
  - The e2e and cache-hit hour medians share one `ring.window(3600)` copy per tick.
  - Front-of-list pruning uses one slice delete (`_trim`) instead of repeated `pop(0)`.
  - Removed unused `_median`, `_hist_quantile`, `_hist_avg` and an unused local.
- **store.py**
  - JSON serialization happens before taking the DB lock; inserts and retention
    pruning share one lock + one commit (was two lock round-trips per save).
  - `history()` stride-downsamples rows *before* `json.loads` (same stride and
    order as before, but decodes ≤800 rows instead of all of them).
  - `synthetic` column migration checks `PRAGMA table_info` instead of a blanket
    `try/except`.
  - Removed unused `save_event`.
- **server.py**: static text assets (≥1 KiB html/css/js/svg/json) are gzipped
  once per file version (mtime+size) for clients sending `Accept-Encoding: gzip`,
  with `Vary: Accept-Encoding`. Decoded bytes are identical. Topic-name regex
  compiled once instead of imported per request.
- **sse.py**: removed unused `Topic.idle_seconds` / `last_ts`.
- **hw.py**: `json` imported at module level instead of every tick.
- **clients.py, session_tokens.py**: removed unused `os` imports.

### Frontend (`web/`)
- Removed CSS rules for classes no page emits: `header.top .nav*`,
  `.btn.danger`, `.card .desc`, `.card .kv*`, `footer.foot`.
- Removed dead JS: `savingsFor()` and `NOW_HUE` (never referenced).
- Cache stamps bumped to `?v=20261026a` (style.css now stamped too).
  `node --input-type=module --check` passes for `dashboard.js` and `charts.js`.

### Measured (fake vLLM engine, same seeded DB, 8 s settle)
| | LIMCARS | LIMCARS-Lean |
|---|---|---|
| dashboard payload on the wire (html+css+js) | 126.6 KB | 41.8 KB (gzip) |
| RSS after boot | ~26.1 MB | ~25.7–25.9 MB |
| startup to first `/api/health` | 0.084 s | 0.084 s |

### Verification
- `/usr/bin/python3 -m pytest tests/ -q` → **44 passed** on the import commit
  and after every change.
- A deterministic sampler replay (fake clock, counter resets, an outage) produced
  byte-identical tick output on old and new code.
- All API endpoints return identical JSON shapes; time-independent endpoints
  (`/api/lost-potential`, `/api/savings-history`, `/api/settings`, …) are
  value-identical on the same seeded DB.
