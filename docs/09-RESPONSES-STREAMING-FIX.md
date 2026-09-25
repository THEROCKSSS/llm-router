# 09 — The Responses-API streaming fix (Codex "OutputTextDelta without active item")

**Symptom.** Codex (or any strict Responses-API client) pointed at LiteLLM's
`/v1/responses` logs, on every reasoning-capable lane:

```
ERROR codex_core::util: OutputTextDelta without active item
```

Sometimes the answer still renders; in bad cases turns complete with
`last_agent_message: null` and the TUI shows nothing.

**Root cause (verified on litellm 1.101.0).** When a lane streams *reasoning
first* (which is exactly what reasoning-capable models do), LiteLLM's
chat→Responses bridge opens an output item for the **reasoning** content:

```
response.output_item.added          <- opens the REASONING item
response.reasoning_summary_text.delta × N
response.output_item.done
response.output_text.delta          <- text deltas for the ANSWER...
response.output_text.done               ...but NO output_item.added ever
response.content_part.done              opened a MESSAGE item for it
```

The flag `sent_output_item_added_event` is single-shot and already `True` from
the reasoning item, so `_ensure_output_item_for_chunk()` returns early at
`if self.sent_output_item_added_event: return` and the text item is never
opened. Strict clients reject text deltas with no active item.

**How to prove it in 10 seconds** — stream with and without reasoning:

```bash
# WITH reasoning (default) -> broken sequence (no message opener)
curl -sN :4000/v1/responses -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"ollama/deepseek-v4.1-flash","input":"hi","stream":true}' \
  | grep -oE '"type":"response\.[a-z_.]+"'

# WITH reasoning disabled -> clean sequence
  -d '{"model":"ollama/deepseek-v4.1-flash","input":"hi","stream":true,"reasoning":{"effort":"none"}}'
```

If the first shows no `output_item.added` before the first `output_text.delta`
but the second does, you have this bug.

**Status upstream.** Fixed on litellm `main` (separate
`sent_message_item_added_event` flag + `_queue_message_item_added_events()`
helper; the related gap-filler work is tracked in BerriAI/litellm PRs #21028 /
#32310, issues #15720 / #20975 / #22102). **NOT in any released version as of
1.102.0** (checked by downloading the wheel and grepping).

## The fix in this repo

`scripts/patch_litellm_responses_stream.py` — a surgical, **idempotent**
backport of upstream's fix onto the *installed* litellm package:

```bash
python scripts/patch_litellm_responses_stream.py          # apply
python scripts/patch_litellm_responses_stream.py --revert # restore pristine
```

It edits `litellm/responses/litellm_completion_transformation/streaming_iterator.py`
(11 hunks: new flag, the queue helper, opener synthesis before text deltas,
`output_index` plumbing, done-event opener, and sync `__next__` ordering) after
writing a `.orig-streamfix` backup and compiling the result before saving.

**Re-run it after EVERY `uv tool upgrade litellm` / litellm reinstall** — an
upgrade overwrites the file and reintroduces the bug. `start-bridge.sh` runs it
automatically before starting the proxy, so normal operation self-heals; the
manual command is for verification.

**Retire this patch once a litellm release contains the fix:**
```bash
python - <<'EOF'
import sysconfig, pathlib
f = pathlib.Path(sysconfig.get_paths()["purelib"]) / "litellm/responses/litellm_completion_transformation/streaming_iterator.py"
t = f.read_text(encoding="utf-8")
print("fix present:", "sent_message_item_added_event" in t)
EOF
```

## Verified result (2026-09-22)

After the patch + proxy restart, the same request produces:

```
response.created
response.in_progress
response.output_item.added          <- reasoning item
response.reasoning_summary_text.delta × N
response.reasoning_summary_text.done
response.reasoning_summary_part.done
response.output_item.done
response.output_item.added          <- MESSAGE item  ← the fix
response.content_part.added         <- the fix
response.output_text.delta          <- now valid: an item is active
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

Pitfall when debugging: a curl without `-N` (or piping through something that
buffers) can make the sequence look wrong. Use `curl -sN` and grep the
`"type":"response.*"` tokens.
