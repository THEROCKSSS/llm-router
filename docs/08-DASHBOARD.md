# 08 — Dashboard & Filtering

The router ships a **filterable live dashboard** — a single page showing every
service, model, key, and agent with instant client-side filtering.

```
http://127.0.0.1:4032            ← local
https://<host>/dashboard         ← via any reverse proxy (e.g. Tailscale Serve)
```

## What it shows

| Tab | Columns | Filterable by |
|---|---|---|
| **Models** | name, lane, upstream id, callable ✓/✗, tokens | search, lane, callable state |
| **Keys** | alias, spend $, model count, wildcard grants | search |
| **Agents** | key alias, tokens, share bar | search |
| **Services** | name, port, up/down | search, status |

Summary cards sit above the tabs: services up/total, model count, tokens and
calls in the window, and the lane distribution.

## Filtering

Three controls, all client-side (instant, no round-trip):

- **Search** — matches model names, upstream ids, agents, services. Press `/` to
  jump to the box from anywhere; `Esc` clears it.
- **Lane select** — laya / openrouter / ollama / your custom lanes / other.
- **Status select** — up-callable only, or problems only.
- **Sort** — by lane, name, or token usage.

The page refreshes every 20 seconds.

## Run it

```bash
python scripts/router_dashboard.py        # serves :4032
```

Stdlib-only (~450 lines). It reads the router API with the **master key** — the
server holds it in memory and never returns it. Endpoints:

| Path | Returns |
|---|---|
| `/` | the dashboard page |
| `/api/summary` | one JSON blob: services, models, keys, spend, counts |
| `/health` | `{"ok": true}` (for autostart/watchdog gating) |

### Performance note

`/spend/logs` on LiteLLM is **slow server-side (~40 s regardless of `limit`)**.
The dashboard:

- fetches a small page (`limit=800`) with a 90 s timeout, and
- **caches the whole summary for 60 s**, so the page itself is instant after the
  first load. The API response includes `cached_age_s` when serving from cache.

If spend shows `spend_error: timed out` while everything else works, the bridge is
busy — the rest of the dashboard is still accurate.

## Wiring it into your stack

The three-layer pattern from `docs/05-OPERATIONS.md`, applied to the dashboard:

1. **Start script** — health-gated start on `:4032` (skip if already answering).
2. **Stop script** — include the port in the kill list.
3. **Watchdog** — add `("dashboard", 4032, "/health")` to the health set so a
   dead dashboard is detected and restarted with everything else.

## Serving other pages too

The same stdlib pattern (`scripts/serve_page.py`) serves any static page or
folder — command references, reports, build output:

```bash
python scripts/serve_page.py --root ./my-commands.html --port 8901
```

Both patterns (dashboard = dynamic JSON page, serve_page = static file) share the
same properties that make them ops-friendly: `/health`, quiet exit when the port
is already held, and localhost-only binding by default.

## Filtering the command-reference page too

The static command reference (`docs`-sibling HTML, served by `serve_page.py`)
carries its own filter bar:

- **search box** — filters command blocks live (matches model, lane, tool, client)
- **lane dropdown** — one option per lane family in your stack plus Ops
- **Clear** — resets both
- section headers auto-hide when all their commands are filtered out, and a
  counter reads `N of M shown`

It's ~60 lines of vanilla JS: tag every command block with `data-lane` +
`data-text` at load, then toggle a `.hidden` class. No framework, no build step,
works from `file://` as well as over HTTP.

## The background-fetch pattern (required for large catalogs)

Both slow endpoints are fetched on **daemon threads** into module-level caches
(`_models`, `_spend`) with TTLs (300s). `/api/summary` NEVER blocks on them: it
returns whatever the cache currently holds and kicks a refresh thread if stale.

Keys (`/key/list` + N `/key/info`) are background-cached too — they queue behind
slow spend queries on the LiteLLM side. A tiny `_kick(cache, fetch, ttl)` helper
does the whole dance: if empty/stale and not already busy, spawn a daemon thread.

Result: cold first call ~0.6s (was 8.6s), warm calls ~0.02s — with ALL blocks
(716 models, 5 keys, 11M-token spend aggregate) landing within ~60s of boot.

Rule: **an endpoint that takes >1s server-side must never be on the page-load
path.** Serve the fast parts first (services, keys) and let the slow blocks land
via the next poll tick (the page auto-refreshes every 20s).
