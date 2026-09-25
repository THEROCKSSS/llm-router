# 10 — Codex old-session resume and Ollama reasoning replay

**Symptom.** An existing Codex rollout is present under
`~/.codex/sessions/...`, but resuming it fails in one of three ways:

1. `Model provider 'router' not found` during TUI bootstrap.
2. Repeated `Reconnecting...` and an HTTP 400 from the router.
3. HTTP 400: `invalid message content type: <nil>` from Ollama.

These are three different failures. The rollout file is not lost in any of them.

---

## 1. Resume with the profile that created the rollout

A Codex rollout records its `model_provider` in the session metadata. A session
created by `codex --profile router` says:

```json
{"model_provider":"router","cwd":"..."}
```

Resuming that UUID with plain `codex` reads the **default** config, which does
not know a provider named `router`, so bootstrap fails before any request is
sent.

```bash
# use the same profile recorded by the session
codex --profile router resume <session-uuid>

# or the wrapper
codex-router resume <session-uuid>

# show sessions from every directory, not just the current cwd
codex --profile router resume --all
```

To confirm the provider from the file itself:

```bash
head -n 1 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
```

If the UUID is unknown, `resume --all` opens the picker. Codex's default picker
filters by current working directory; both the `--all` flag and the recorded
`cwd` matter when someone says "my session disappeared".

---

## 2. Replayed reasoning items and Ollama's null-content rejection

Codex sends the complete prior turn history on resume. The Responses API stores
reasoning as separate `reasoning` input items:

```json
{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text","text":"..."}],"content":null}
```

LiteLLM's Responses-to-chat bridge turns each into:

```json
{"role":"assistant","content":null,"reasoning_content":"..."}
```

Ollama's OpenAI-compatible endpoint rejects that shape when the message has no
tool calls. The minimal matrix, captured against the live router:

| assistant message | result |
|---|---|
| `content=None` + reasoning, no tool calls | **400** `invalid message content type: <nil>` |
| `content=""` + reasoning, no tool calls | **200** |
| `content=None` + reasoning + tool calls | **200** |

So the failure is specific to reasoning-only assistant turns — exactly what a
long reasoning session accumulates.

**10-second replay test** (builds no Codex client, no GPU):

```bash
curl -s http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"ollama/deepseek-v4.1-flash","input":[
    {"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]},
    {"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text","text":"thinking..."}],"content":null},
    {"type":"message","role":"user","content":[{"type":"input_text","text":"say OK"}]}
  ]}'
```

Before the patch this returns the `<nil>` error. After it, the proxy forwards a
valid request; the actual HTTP result then depends on that provider's quota
(verified again on 2026-09-24, the live Ollama key had reached its weekly limit).

---

## The patch

`scripts/patch_litellm_null_content.py` edits
`litellm/responses/litellm_completion_transformation/transformation.py` and
coerces only the problematic messages:

```python
if (
    message.get("role") == "assistant"
    and message.get("content") is None
    and not message.get("tool_calls")
):
    message["content"] = ""
```

Properties:

- **Idempotent** — a marker in the file makes re-runs a no-op.
- **Portable** — resolves the active interpreter's `purelib`; no user paths.
- **Backed up** — `.bak-llmrouter-null-content` before the first write.
- **Compile-checked** before writing.
- **Reversible** — `--revert` restores the pristine file.
- **Self-healing** — `start-bridge.sh` reapplies it before the proxy starts,
  because any LiteLLM upgrade overwrites the installed package file.

Run or test it:

```bash
python scripts/patch_litellm_null_content.py
python scripts/patch_litellm_null_content.py --revert
python scripts/test_null_content_patch.py
```

The regression test does not import or modify the installed LiteLLM. It applies
the patch to a minimal fixture with the same method layout and asserts:

1. null + reasoning, no tool calls becomes `""`;
2. existing `""` stays `""`;
3. null + tool calls is untouched;
4. applying the patch twice reports no further change.

**Retire the patch** when the installed LiteLLM contains an equivalent fix:

```bash
grep -R "_llmrouter_coerce_null_assistant_content" \
  "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/litellm/responses"
```

If a future LiteLLM release fixes the behavior upstream, this script prints
`ABORT` and the launcher logs a warning instead of corrupting the new file.

---

## Verified result

- `/v1/responses` with a replayed reasoning item: the request passes the
  provider-shape check; the live Ollama provider then returned its independent
  weekly quota 429, not the former `content: <nil>` 400.
- A fork of a large real agent session (4.9 MB, hundreds of reasoning items)
  resumed through `codex --profile router` and produced a normal answer.
- The **original** rollouts then resumed successfully on a free, tool-capable
  1M-context lane: session A returned `A_SESSION_OK_RETRY` after
  1,743,239 tokens; session B returned `B_SESSION_OK` after 622,496 tokens.
- Lane caveat observed during verification: lanes capped at 131,072 tokens
  reject these histories (~187k and ~212k prompts) with 413, and a provider
  quota cap can independently return 429. Neither condition is a
  session-integrity failure; resume long sessions on a lane whose context
  window actually fits them.
- The original rollout was preserved; verification used a fork, then the test
  turn was removed from the original rollout with a full backup retained.
