---
name: litellm-decision-engines
description: "Use when wiring typed-decision engines (Jev, Laya) onto a chat-completions router — non-chat APIs, question types (choice/score/noul), adapters, and the local Laya deployment."
version: 1.0.0
author: Owen
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [jev, laya, decisions, noul, choice, score, local-models]
    related_skills: [litellm-multi-lane-router, litellm-router-operations]
---

# Decision Engines on a LiteLLM Router (Jev + Laya)

## Overview

Decision engines answer **typed questions about a state** — returning labels,
levels, and calibrated probabilities — instead of generating text. Two engines,
same question shapes, different homes:

- **Jev** — cloud API, `systemone` endpoint (NOT chat), per-token cost.
- **Laya** — local, Apache-2.0 (`pip install laya`), ~30 ms on a modest GPU, $0.

Both become ordinary lanes through adapters, so any chat client can use them.

## The three question types (both engines)

| Type | Output | Use for |
|---|---|---|
| `choice` | label + probability per option | routing, classification |
| `score` | expected level on an ordinal rubric + distribution | severity, urgency |
| `noul` | calibrated P(true) 0–1 | yes/no: churn risk, guards, "worth doing?" |

`choice.criteria` must be a **dict** `{key: description}` (a list → 422).
`score.criteria` is an ordered **list**. `noul` takes instructions only.

## Jev (the adapter pattern)

Jev's endpoint takes `{state, questions}`; it cannot chat. The adapter:

- **In:** last user message = the state. JSON `{"state":..., "questions":{...}}`
  used verbatim; otherwise defaults (`worth_doing` noul + `confidence` score).
- **Out:** rendered text, e.g. `• worth_doing: YES (96% yes)`.

Implementation pattern: in the handler, detect the decision model, build the
typed payload (state + questions), call the engine, and render the answers as
text. The same pattern works in any proxy layer: translate chat→typed request
in, render typed answers out. `scripts/laya_server.py` is a working example of
the whole loop for a local engine.

If a provider's free access sits behind a request-shape gate, the same
CustomLLM handler is where you build the required shape — see the
custom-provider lane in `litellm-multi-lane-router`. Daily caps often reset at
00:00 UTC; a 429 is quota, not death.

## Laya (local deployment)

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe torch --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv/Scripts/python.exe laya
.venv/Scripts/python.exe laya_server.py     # :4030; cold preload ~90-180s, then ~30ms
```

Router lanes: `laya` (auto-routed), `laya-en`, `laya-ml` (100+ langs), `laya-typed`.
The wrapper (repo `scripts/laya_server.py`) exposes OpenAI chat + native
`POST /predict` for raw JSON answers.

## Iron rules

1. **Keep both adapters rendering IDENTICALLY** (`• qid: YES (62% yes)`) — then a
   side-by-side comparison shows engine differences, not plumbing differences.
2. **A local decision server MUST support `stream: true`** — Codex/Hermes stream
   by default and read an EMPTY response (`finish_reason=stop`, no content)
   when SSE is missing. Emit content chunk + finish + `[DONE]`.
3. **Never evict a model on a 429.** Classify probes OK/RATE/DEAD/UNKNOWN;
   only 404/410 removes.

## Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Jev returns nothing via chat | Not a chat model | Keep the adapter in the request path |
| `choice` → 422 | criteria list instead of dict | Use `{key: description}` |
| Laya first call takes minutes | Cold preload | Wrapper binds port first + background preload; wait, don't restart |
| Agent answers empty via decision lane | Server ignored `stream:true` | Implement SSE |
| Engine flapping off keys each refresh | 429 treated as death | RATE = keep semantics |
| Other lanes stripped from keys | Refresh rebuilt keys from own list only | Union all registered names |
| `>20` choice options inaccurate (Laya) | Token budget per option | Raise `head_max_len`/`max_len`, or shortlist first |

## Verification checklist

- [ ] `curl -N ... '{"stream":true}'` shows a **content** delta (not `"delta":{}`)
- [ ] Plain yes/no question answered with a noul line
- [ ] Structured JSON `{state, questions}` accepted
- [ ] Client E2E: at least one CLI agent answered through the decision lane
- [ ] Comparison script runs both engines on one state without errors
