# 06 — Pitfalls Index

Every entry here cost real debugging time. They're grouped by where they bite.

---

## Config & routing

| Symptom | Cause | Fix |
|---|---|---|
| One lane 400s on a param others accept | Clients send params (e.g. `reasoning_effort`) the lane doesn't know | `drop_params: true` in `litellm_settings` — per-lane silent drop |
| 401 with a key that worked before | Virtual key's plaintext is lost (only the hash is stored) | Regenerate with `/key/generate` |
| 403 "key not allowed to access model" | Key allowlists are fixed at mint/update time | `POST /key/update` with the new model added |
| New lane works via master key, 403 via agent keys | Same as above | Same fix — **after every lane addition, update every key** |
| `/v1/models` lists hundreds of phantom models | Provider cost-map noise, not the provider's real catalog | Probe real model ids before advertising |
| Wildcard lane models missing from `/v1/models` | Wildcards don't enumerate by design | Call them by name; use a sync script for discovery |
| Empty content from `/v1/messages` | Anthropic-bridge path drops text without a flag | `use_chat_completions_url_for_anthropic_messages: true` |
| DB connection refused with correct creds | `localhost` resolved to IPv6, Postgres on IPv4 | Use `127.0.0.1` everywhere |
| Readiness `db:disconnected` right after start | Query-engine first-cold-connect flake | Restart the proxy once (automate in the launcher) |
| LiteLLM dies instantly with `UnicodeEncodeError: 'charmap' codec can't encode` in `show_banner()` | cp1252 Windows console + Unicode banner — process dies before it listens; clients see "Reconnecting… Connection failed" | Launch with `PYTHONIOENCODING=utf-8` (start script + every launcher) |
| `choice` question → 422 | `criteria` given as a list | `criteria` must be a `{key: description}` dict |

## Client-side

| Symptom | Cause | Fix |
|---|---|---|
| Agent answers empty, log says `finish_reason=stop` with no content | Client streams (`stream: true`); server ignored it and returned plain JSON | Make the local server emit SSE when asked: content chunk + finish + `[DONE]` |
| Codex 400 "cannot unmarshal object…" | Codex sends `reasoning: {effort, summary}`; string-typed upstreams reject it | `model_reasoning_summary = "none"` in the Codex profile |
| `Model metadata for '<x>' not found` | Custom model names aren't in Codex's registry | Harmless — ignore |
| Agent "works" but instantly, with old behavior | Stale process still holding the port with old config | Kill the **actual PID** owning the port (netstat/powershell), then restart |
| Hermes one-shot hangs on first run | Full profile load (~30–60 s) | Wait; subsequent runs are fast |
| Codex log spams `failed to refresh available models: timeout waiting for child process to exit` | Upstream Codex bug (openai/codex#23119, #34397) — the background model-catalog refresh's child process times out; fires on default configs, unrelated to the router | Point Codex at a static catalog: `model_catalog_json = "C:\path\models_catalog.json"` (shape `{"models":[...]}`, same schema as `~/.codex/models_cache.json`) |
| Codex log: `OutputTextDelta without active item` | LiteLLM's chat→Responses bridge never opens a message item when a lane streams reasoning first (one-shot opener flag consumed by the reasoning item); fixed upstream but in no release ≤1.102.0 | Backport patch (`scripts/patch_litellm_responses_stream.py`) + re-run after every litellm upgrade — see `docs/09-RESPONSES-STREAMING-FIX.md` |
| Codex old session resumes with repeated reconnects / HTTP 400 `invalid message content type: <nil>` | Replayed Responses `reasoning` items become assistant messages with `content=None`; Ollama rejects null assistant content unless tool_calls are present | Apply `scripts/patch_litellm_null_content.py`; the start script reapplies it. Test with `scripts/test_null_content_patch.py` and replay a reasoning item through `/v1/responses` |
| `Model provider 'router' not found` while resuming an old Codex session | The rollout was recorded under `--profile router`, but the command resumed from the default profile, whose config does not know that provider name | Run `codex --profile router resume <uuid>` (or `resume --all` for the picker) |
| Laya `/health` is 200 but predictions hang | The wrapper binds first and loads GPU checkpoints in a background thread; port health does not mean the model is ready | Gate startup/watchdogs on `/info` containing `laya_version`; expect multi-minute cold loads |

## Containers & runtime

| Symptom | Cause | Fix |
|---|---|---|
| Containers "running" after VM restart but ports dead | systemd abort-killed the boot restore, taking port-forwarders with it | Hardened `podman-restart.service` drop-in (`docs/05-OPERATIONS.md`) + port healer |
| `podman ps` hangs (no output, exit 124) | Wedged client/VM API | `podman machine stop` + `start`; the boot restore brings containers back |
| "address already in use" with no visible listener | Orphaned rootlessport inside the runtime's netns | Recreate the container on a fresh port using the **same volume**; don't fight the orphan |
| Container data "lost" after re-create | Wrong volume reattached (name collision vs hash-named volume) | `podman volume inspect` both before re-creating |
| `podman network reload` refused | Remote-client limitation on Windows | Run it inside `podman machine ssh`, or just stop/start the container |

## Paths, shells, scheduling

| Symptom | Cause | Fix |
|---|---|---|
| Scheduled script dies with `C:UsersUser…` mangled paths | A `.sh` cron job ran under the WSL bash, which eats backslashes | Make scheduled scripts Python, or call Git Bash by absolute path |
| Native tool can't find a file that exists in git-bash | MSYS path (`/c/...`) passed to a native binary | Pass the `C:/...` form explicitly to native tools |
| Literal `***` appears in a file you wrote | Display-layer secret masking corrupted a written string | Build such strings at runtime (`"Bearer" + " " + key`) |
| Cron: "no job control in this shell" | Backgrounded process under a non-interactive shell | Expected noise; verify via logs/health endpoints instead |

## Decision engines

| Symptom | Cause | Fix |
|---|---|---|
| Jev returns nothing via chat | It's not a chat model (`/systemone` only) | Use/keep the adapter (`docs/03-JEV-AND-LAYA.md`) |
| Free decision tier 429s in bursts | Daily quota caps (reset 00:00 UTC) | Retry after reset; keep a local lane as backup |
| Laya first request takes minutes | Cold checkpoint preload (~90–180 s) | Bind port first + background preload (wrapper does this); later calls ~30 ms |
| Refresh evicts a working model | A quota 429 was treated as "dead" | Classify probe results: RATE = keep, DEAD = remove only on 404/410 |
| Refresh strips other lanes from keys | Key sync rebuilt from only its own models | Union with all registered model names when updating keys |

## Security hygiene

| Symptom | Cause | Fix |
|---|---|---|
| A key committed to git "just once" | Config convenience | Rotate it immediately; keys belong in env/secret files, never repos |
| Every client uses the master key | Laziness | One virtual key per client — revoke/spend-track individually |
| Proxy reachable from the LAN unexpectedly | Bound `0.0.0.0` | Bind `127.0.0.1`; expose deliberately (reverse proxy + TLS) if needed |
