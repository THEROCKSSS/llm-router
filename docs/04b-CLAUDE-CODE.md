# 04b — Claude Code via the Router

Claude Code speaks Anthropic's Messages API. LiteLLM exposes `/v1/messages`, so
Claude Code can run on **any router lane** — cloud, free, or local — by pointing
two environment variables at the gateway. Verified working against a multi-lane
router (several cloud, free, and local lanes tested).

---

## The two variables

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"      # the router
export ANTHROPIC_AUTH_TOKEN="$(cat key_claude)"        # a router virtual key
```

Plus one model selection:

```bash
export ANTHROPIC_MODEL="qwen3.8-flash"                 # any lane/model name
```

Your normal Anthropic subscription config is untouched — these are per-process.

## Required router setting

```yaml
litellm_settings:
  use_chat_completions_url_for_anthropic_messages: true
```

**Without it `/v1/messages` returns empty content** — LiteLLM's Responses-bridge
path drops the text when translating back to Anthropic shape. This is the single
most common "Claude Code answers with nothing" cause.

## The launcher pattern

A wrapper that (1) sources keys, (2) ensures the stack is up, (3) exports the
gateway vars, (4) takes an **optional model as the first argument** — and
forgivingly falls back to the default if the caller starts with a flag instead:

```bash
#!/usr/bin/env bash
# launch-router.sh — Claude Code through the local LLM router
set -euo pipefail

: "${ANTHROPIC_AUTH_TOKEN:=$(cat /path/to/key_claude)}"

# optional: start the bridge if it isn't listening
if ! curl -s -o /dev/null -m 2 http://127.0.0.1:4000/health/liveliness; then
  echo "[launch] starting the router ..."
  # ... your start script ...
fi

export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN
# Our lane names aren't in Claude Code's model registry; declare the real
# context window so it doesn't assume 200k and auto-compact early.
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-131072}"

# Model is OPTIONAL and must be a bare name (no leading dash). If the caller
# starts with a flag (-p, --print, ...) we assume they meant the default lane
# and pass everything through — otherwise the flag gets read as a model name
# and Claude Code dumps its whole gateway model list back at you.
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  export ANTHROPIC_MODEL="$1"
  shift
else
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-<your-default-lane>}"
fi

exec claude "$@"
```

Usage — **model first, then Claude Code's own flags**:

```bash
launch-router.sh                                  # interactive, default lane
launch-router.sh upstream-a/fast -p "say OK"      # one-shot
launch-router.sh ollama/deepseek-v4.1-flash -p "task"
```

> **Gotcha (handled by the launcher above):** if the first argument is a flag
> like `-p`, treat it as "no model given" and pass everything through. A naive
> wrapper (`ANTHROPIC_MODEL="$1"; exec claude "${@:2}"`) instead reads `-p` as
> the model name and Claude Code dumps its full gateway-model list
> (`Tried to access -p ...`). Both invocation styles then work:
> `launch-router.sh -p "hi"` (default lane) and
> `launch-router.sh upstream-a/fast -p "hi"` (explicit lane).

## Model registry warning is benign

```
"qwen3.8-flash" is not a model this version of Claude Code recognizes ...
```

Claude Code keeps a registry of known models for context-window sizing. Custom
lane names aren't in it, so it assumes 200k tokens. Harmless, but it can trigger
early auto-compaction. Fixes:

- `CLAUDE_CODE_MAX_CONTEXT_TOKENS=131072` (what the launcher above does) — or your
  lane's real window.
- Or append `[1m]` to the model name for a 1M-token lane.
- Or map the name in Claude Code's `modelOverrides` setting.

## Tool use works — verify it

Claude Code sends tool definitions; routers must deliver Anthropic `tool_use`
blocks back. Probe it directly:

```bash
curl -s http://127.0.0.1:4000/v1/messages \
  -H "x-api-key: $(cat key_claude)" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-flash","max_tokens":200,
       "messages":[{"role":"user","content":"Weather in Paris? Use the tool."}],
       "tools":[{"name":"get_weather","description":"Get weather",
                 "input_schema":{"type":"object",
                   "properties":{"city":{"type":"string"}},"required":["city"]}}]}'
```

Expect a `content` array containing `{"type":"tool_use","name":"get_weather",
"input":{"city":"Paris"}}` and `stop_reason":"tool_use"`.

> **Chat-only models can't do this.** A lane whose upstream has no tool-capable
> provider returns `404 "No endpoints found that support tool use"` — see
> `docs/06-PITFALLS.md`. Verify tool support before assigning a model to
> Claude Code (or any agent).

## Reasoning/thinking lanes

Claude Code sends extended-thinking parameters on some flows
(`MAX_THINKING_TOKENS`). Lanes that emit reasoning (ollama and most reasoning
> models) return
`thinking` content blocks — verified. Lanes that don't simply ignore the setting;
`drop_params: true` on the router keeps that from erroring.

## Checklist

- [ ] `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` point at the router and a
      **client key** (not the master key)
- [ ] `use_chat_completions_url_for_anthropic_messages: true` is set
- [ ] A one-shot `-p` answers with real text on the default lane
- [ ] `CLAUDE_CODE_MAX_CONTEXT_TOKENS` set (or `[1m]` suffix) to silence the
      registry warning and avoid early compaction
- [ ] Tool probe returns a `tool_use` block on the lanes you'll actually use
- [ ] Spend for the `claude` key shows in the dashboard after a session
