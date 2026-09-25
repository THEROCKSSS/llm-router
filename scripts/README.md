# Scripts

Working scripts extracted from a production setup. All sanitized — no keys, no
personal paths. Read each file's docstring for the full story.

| File | What it does | When you need it |
|---|---|---|
| `laya_server.py` | Local HTTP server wrapping the **Laya** decision engine (OpenAI chat + native `/predict`, background preload, SSE) | You want a local decision lane (`docs/03-JEV-AND-LAYA.md`) |
| `heal_container_ports.py` | Finds containers whose published ports died and rebuilds them (`stop -t 15` + `start`) | After a VM/container-runtime restart (`docs/05-OPERATIONS.md`) |
| `compare_decisions.py` | Runs one state through two decision lanes and prints both — identical rendering | Comparing Jev vs Laya, or validating a swap |
| `test_matrix.py` | Full lane sweep: plain + tool-calling per model, writes `test-matrix-results.json` | After changing lanes/keys, or when an agent reports odd failures |
| `test_responses_api.py` | Codex wire-path sweep (`/v1/responses` + tools) — proves which models can host agent tool-calls | Before recommending a model for Codex/Hermes; catches chat-only models |
| `refresh_registry_example.py` | Skeleton for a dynamic model-registry refresh: probe → classify OK/RATE/DEAD/UNKNOWN → register/remove → sync keys | You manage models at runtime instead of in YAML |
| `direct_shim_example.py` | Minimal OpenAI-shape shim in front of a non-OpenAI vendor API | Your upstream isn't OpenAI-compatible (Pattern 1, `docs/02-LANES.md`) |
| `serve_page.py` | Stdlib static page/folder server with `/health`, idempotent startup (safe in autostart), 127.0.0.1-only by default | You built a reference page/dashboard and want it at a stable URL — pair with a reverse proxy (`<proxy>: /ref → http://127.0.0.1:8901`) to reach it from other devices |
| `patch_litellm_responses_stream.py` | **Idempotent backport** of upstream's message-item opener fix for `/v1/responses` streaming (fixes Codex `OutputTextDelta without active item`). Re-run after every litellm upgrade; `--revert` restores the pristine file | Any reasoning-capable lane behind LiteLLM + a strict Responses-API client (Codex) |
| `patch_litellm_null_content.py` | **Idempotent patch** that changes replayed reasoning-only assistant messages from `content=None` to `content=""` (Ollama rejects null content); `--revert` restores pristine | Old Codex sessions containing reasoning items fail to resume on an `ollama/*` lane |
| `test_null_content_patch.py` | Fixture-based regression test for the null-content patch: verifies the three relevant message shapes and idempotent reapplication | After touching the patch or upgrading LiteLLM |
| `router_dashboard.py` | **Filterable live dashboard** — services, models (lane/callable), keys, agent spend, with search + lane/status filters; caches the slow `/spend/logs` call | You want one screen showing the whole router's live state (`docs/08-DASHBOARD.md`) |
| `start-bridge.sh` | Generic launcher: DB container → self-healing LiteLLM patches → wait → proxy → local servers; distinguishes port health from model/database readiness | Standing the stack up / autostart |

## Conventions used here

- **Python for anything scheduled.** Shell scripts are fine for manual runs, but
  scheduled jobs on Windows should be `.py` (the `.sh`-under-WSL-bash path bug —
  see `docs/06-PITFALLS.md`).
- **Absolute paths for native tools.** In git-bash, pass `C:/...` forms to native
  binaries (Python, node); MSYS `/c/...` paths get mangled.
- **Secrets via env or files read at start.** Scripts here read keys from
  environment variables; the launcher sources `.env` (`set -a; . ./.env; set +a`).

## Adapting these to your layout

Every script takes its config via environment variable or CLI flag with a sane
default. The only thing you'll usually change is the port numbers and the
provider URLs. If a path is baked in (e.g. log location), it's marked with a
`# EDIT:` comment.
