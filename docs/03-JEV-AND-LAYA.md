# 03 — Decision Engines: Jev and Laya

Most models on a router are chat models: you send messages, you get text back.
**Decision engines are different** — you send a *state* (some text/JSON to judge)
and *typed questions*, and you get **typed answers with calibrated probabilities**
instead of prose. No generation, nothing to parse, nothing to hallucinate.

Two engines, both documented here:

| | **Jev** | **Laya** |
|---|---|---|
| Where it runs | Cloud API (closed) | **Your machine** (Apache-2.0) |
| Endpoint | Custom (`/systemone`) — not OpenAI chat | Via a local wrapper this repo provides |
| Latency | ~240–280 ms p50 (third-party) | **~30 ms** (GPU), ~200–500 ms through the router |
| Cost | Per-token, $0.042/1M | $0 self-hosted |
| Languages | English-focused | 45/51 usable (router picks checkpoints) |
| Calibration | Raw ECE ~0.14 | Better after temperature fitting; stronger raw argmax |
| High-cardinality choice (>20 options) | **Strong** | Weaker at default token budgets (fix: raise `head_max_len`, or shortlist) |
| Install | API key | `pip install laya` |

They answer the same question shapes, so this repo treats them as siblings and
includes a side-by-side runner (`scripts/compare_decisions.py`).

---

## The three question types (both engines)

| Type | Output | Use for |
|---|---|---|
| `choice` | Label + probability per option | Routing, classification, intents |
| `score` | Expected level on an ordinal rubric + distribution | Severity, urgency, sentiment strength |
| `noul` | Calibrated P(true), 0–1 | Yes/no decisions: churn risk, phishing, jailbreak, "is this worth doing?" |

Example question set:

```json
{
  "urgency": {
    "type": "choice",
    "instructions": "How urgent is this request?",
    "criteria": {
      "not": "no rush",
      "soon": "within days",
      "critical": "blocking issue or explicit threat"
    }
  },
  "severity": {
    "type": "score",
    "instructions": "How severe is the issue?",
    "criteria": ["low", "medium", "high"]
  },
  "churn_risk": {
    "type": "noul",
    "instructions": "Does the user threaten to cancel or leave?"
  }
}
```

> `choice` criteria is a **dict** `{key: description}` (a list is a common 422
> error). `score` criteria is an ordered **list**. `noul` needs only instructions.

### Cold start and readiness

The wrapper binds the port first, then loads checkpoints in a background thread.
This avoids a service that is "up" for minutes while every prediction is queued.
The trade-off is that `/health` becomes 200 **before inference is available**.
Gate launchers and watchdogs on `/info` instead:

```bash
curl -s http://127.0.0.1:4030/info
# ready only after this contains: "laya_version": "..."
```

Cold loading varies with GPU, cache, and checkpoint count (observed between ~1
and ~7 minutes). Clients that timeout during preload are harmless; the server
ignores their socket disconnects so real startup errors remain visible.

---

## Jev on the router

Jev is **not chat-capable** — its endpoint is `/systemone`, taking
`{state, questions}` and returning typed answers. To make it behave like every
other model on the router, a small adapter inside the custom-provider handler
(`docs/02-LANES.md` → Pattern 5) does this translation:

- **Inbound:** the last user message is the state. If it's JSON
  `{"state": ..., "questions": {...}}`, both are used verbatim; otherwise a
  default question set (`worth_doing` + `confidence`) is attached.
- **Outbound:** typed answers are rendered as text:

```
Jev decision (jev-1.13)
• worth_doing: YES (96% yes)
• confidence: 1.84/2.0  [low 5%, medium 20%, high 75%]
```

So any chat client can use it — the answers just read as structured lines.

**Pitfalls:**

- `criteria` for `choice` must be a dict; a list returns 422.
- Free access to Jev may come through a gated provider — the gate validates
  *request shape*, which is exactly why the custom provider exists
  (`docs/02-LANES.md` → Custom providers).
- Daily free-tier caps reset at 00:00 UTC — a 429 at the cap is not a failure;
  retry after reset or keep a second lane.

---

## Laya on the router (fully local)

[Laya](https://github.com/NandhaKishorM/laya) is a non-autoregressive decision
engine: ModernBERT-based encoders, one forward pass, ~30 ms per question on a
mid-range GPU (an RTX 3060 is plenty). It ships three checkpoints and a `Router`
that picks between them per request:

| Checkpoint | Use for |
|---|---|
| `laya` (router) | auto-selects per request — best default |
| `laya-en` / english | English text, best English accuracy |
| `laya-ml` / multilingual | 100+ languages, detects script/language first |
| `laya-typed` | the typed-decisions workflows |

### Install

```bash
# dedicated venv, CUDA torch
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe torch \
    --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv/Scripts/python.exe laya
```

> The torch index URL matters: install torch **first** from the CUDA index,
> then everything else. Sizes: torch ≈ 2.6 GB download; the checkpoints ≈ 2.5 GB
> on first run (cached in `~/.cache/huggingface`).

### Run it behind the router

`scripts/laya_server.py` (this repo) is the wrapper: stdlib HTTP server on
`127.0.0.1:4030`, `Router(preload=True)` on the best GPU, OpenAI chat endpoint +
a native `POST /predict` that returns raw JSON answers.

```bash
.venv/Scripts/python.exe laya_server.py        # cold preload ~90–180 s; after that ~30 ms
```

Router lanes (already in `config.example.yaml`): `laya`, `laya-en`, `laya-ml`,
`laya-typed`.

### Using it from a chat client

Same input convention as Jev — last user message is the state:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"laya","messages":[{"role":"user","content":"Yes or no: should we ship this?"}]}'
```

```
Laya decision (laya / routed: english)
• worth_doing: YES (61% yes)
• confidence: 1.27  [low 7%, medium 60%, high 34%]
```

Or pass custom questions as JSON in the message:

```json
{"state": "...your text...", "questions": { ...as shown above... }}
```

Native (unwrapped) access, when you want raw probabilities:

```bash
curl http://127.0.0.1:4030/predict -H "Content-Type: application/json" \
  -d '{"state": "the text to judge", "questions": {...}}'
```

### Laya-specific pitfalls

1. **Cold preload is slow (90–180 s).** The wrapper binds the port first and
   preloads in the background — health checks pass immediately, requests wait.
2. **Streaming clients need SSE.** Codex/Hermes stream by default; a server that
   ignores `stream: true` makes clients read an empty response. The wrapper
   emits a content chunk + finish + `[DONE]`.
3. **The engine's default question set is generic** (`worth_doing` +
   `confidence`). For real work, send explicit questions per use case.
4. **>20 choice options lose accuracy** at default token budgets — raise
   `head_max_len` or shortlist candidates first (see Laya's README).
5. **Confidence is calibrated-ish, not magic.** Treat 0.5–0.6 noul reads as
   "lean" rather than "decided", and gate automation on higher thresholds.

---

## Comparing the two (side-by-side)

`scripts/compare_decisions.py` runs one state through both engines on the router
and prints both rendered decisions:

```bash
python compare_decisions.py
python compare_decisions.py --state "Your text to judge"
python compare_decisions.py --state "..." --questions-json my-questions.json
```

Use it whenever you consider swapping engines for a workflow — the outputs are
rendered identically on purpose, so the differences you see are the engines, not
the plumbing.

## Which should you use?

- **Privacy / cost / offline** → Laya (local, $0, nothing leaves the machine).
- **Latency-critical** → Laya (~30 ms vs ~250 ms).
- **Huge label sets (50+ options)** → Jev, or Laya with raised token budgets.
- **Zero-setup** → whatever provider you already have a key for; both are
  optional lanes on this router.
