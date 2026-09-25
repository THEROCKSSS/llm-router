# 04 — Wiring Clients

Every client below speaks the same OpenAI protocol at
`http://127.0.0.1:4000/v1` with **its own virtual key** — never the master key.

---

## Codex CLI

Codex supports named profiles that *layer on top of* your main config —
`codex --profile <name>` uses `~/.codex/<name>.config.toml` while your default
config stays untouched. That's the clean way to point Codex at the router.

**1. Create `~/.codex/router.config.toml`:**

```toml
model = "openai-compatible/<your-default-lane>"
model_provider = "router"
model_context_window = 1048576
model_reasoning_effort = "high"
# See pitfall below — keep this at "none" unless your lanes accept summaries.
model_reasoning_summary = "none"

[model_providers.router]
name = "Local LLM Router"
base_url = "http://127.0.0.1:4000/v1"
env_key = "LLM_ROUTER_CODEX_KEY"        # the codex virtual key
wire_api = "responses"
```

**2. Provide the key** — put `LLM_ROUTER_CODEX_KEY=<codex virtual key>` in
`~/.codex/.env` (or your environment).

**3. Wrappers:**

```bash
# ~/.local/bin/codex-router  (bash)
exec codex --profile router "$@"
```

```bat
:: ~/.local/bin/codex-router.bat
@echo off
codex --profile router %*
```

**4. Run it:**

```bash
codex-router                          # interactive
codex-router exec "fix the failing test"   # one-shot
codex-router -m <other-lane> exec "…"      # switch lanes per run
```

### Codex pitfalls

0. **Old sessions made under `--profile router` need that same profile.**
   A rollout records its `model_provider` (for example `router`). Resuming it
   from the default profile fails during TUI bootstrap with
   `Model provider 'router' not found`, even though the rollout file is intact.
   Resume with:
   ```bash
   codex --profile router resume <session-uuid>
   # picker across all directories when IDs are unknown:
   codex --profile router resume --all
   ```
   If you deliberately want default-profile sessions to open router rollouts,
   the base config may also define `[model_providers.router]`. That is broader
   than it looks: the main profile still chooses its own default model/provider,
   so use the router profile when you want the lane from the recorded session.
1. **Reasoning summaries break some lanes.** Codex sends
   `reasoning: {effort, summary:"auto"}` to `/v1/responses`. Upstreams that map
   that onto a plain string 400 with a "cannot unmarshal object" error. Fix:
   `model_reasoning_summary = "none"` in the profile. (With `drop_params: true`
   on the proxy, tolerant lanes silently drop it either way.)
2. **Model metadata warnings are noise.** Codex prints
   `Model metadata for '<lane>' not found` for every custom model name — harmless.
3. **Run `exec` inside a git repo** or Codex will complain about trust.

---

## Hermes Agent

Hermes profiles are self-contained (config + keys + skills). Point one at the
router as a **custom provider**, with a fallback to a direct provider so the
agent survives the router being down.

```bash
# provider + model
hermes -p myprofile config set providers.router.name "Local LLM Router"
hermes -p myprofile config set providers.router.base_url "http://127.0.0.1:4000/v1"
hermes -p myprofile config set providers.router.key_env "LLM_ROUTER_HERMES_KEY"
hermes -p myprofile config set providers.router.api_mode "chat_completions"
hermes -p myprofile config set providers.router.default_model "<your-default-lane>"
hermes -p myprofile config set model.provider "custom:router"
hermes -p myprofile config set model.default "<your-default-lane>"
hermes -p myprofile config set model.api_mode "chat_completions"

# fallback to a direct provider (ollama-cloud shown — use any you have a key for)
hermes -p myprofile config set fallback_providers \
  '[{"provider":"ollama-cloud","model":"<a-model-you-can-reach-directly>"}]'

# optional aliases for quick switching
hermes -p myprofile config set model_aliases.fast '{"model":"<lane>","provider":"custom:router"}'
hermes -p myprofile config set model_aliases.decide '{"model":"laya","provider":"custom:router"}' --force
```

Put the key in the profile's `.env`: `LLM_ROUTER_HERMES_KEY=<hermes virtual key>`.

Verify: `hermes -p myprofile chat -q "Reply with exactly: OK" -Q`

> **Note:** `-Q` gives a quiet one-shot. First run after edits loads the whole
> profile (can take ~30–60 s); later runs are fast.

---

## Claude Code

Claude Code talks to Anthropic's API shape. LiteLLM exposes `/v1/messages`, so
point Claude Code's base URL at the router:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=<claude virtual key>
export ANTHROPIC_MODEL=<a-lane-that-handles-chat>
claude -p "say OK"
```

> `use_chat_completions_url_for_anthropic_messages: true` must be in
> `litellm_settings` (it is, in `config.example.yaml`) or `/v1/messages`
> responses come back with empty content.

Wrap it in a launcher script (`scripts/` shows the pattern) so nobody types the
env block by hand.

---

## OpenCode (and any OpenAI-compatible tool)

OpenCode config lives at `~/.config/opencode/opencode.jsonc` — add the router as
a provider:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "router": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local LLM Router",
      "options": { "baseURL": "http://127.0.0.1:4000/v1", "apiKey": "<key>" },
      "models": { "<lane>": { "name": "<lane>" } }
    }
  },
  "model": "router/<lane>",
  "small_model": "router/<cheap-fast-lane>"
}
```

Any other tool — SDK, IDE plugin, curl script — is the same recipe:
base URL `http://127.0.0.1:4000/v1`, Bearer = a virtual key, model = a lane name.

---

## Key strategy

- One virtual key **per client** (`coder`, `agent`, `scripts`) — spend is
  attributed per key, and you can rotate/revoke one without touching others.
- Grant only the lanes each client needs (`models: [...]` allowlist).
- **After adding a new lane, update all keys** — keys don't auto-include new
  models.

## Cross-client checklist

- [ ] Each client uses its own virtual key (not the master key)
- [ ] `codex-router -m <lane> exec "…"` answers on at least two lanes
- [ ] Hermes `-q` one-shot answers through the router
- [ ] Claude Code `-p "say OK"` answers (if used)
- [ ] Router down → fallback path still answers (Hermes fallback / direct URL)
