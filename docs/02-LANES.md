# 02 — Adding Lanes

Every lane is an entry in `model_list` (or, for custom providers, a handler +
`custom_provider_map`). This page covers each pattern with real, working shapes.

---

## Pattern 1 — Direct upstream (OpenAI-compatible)

The simplest case: the upstream speaks plain OpenAI chat completions.

```yaml
- model_name: my-vendor/fast              # what clients call
  litellm_params:
    model: openai/<the-vendors-model-id>  # what the upstream calls it
    api_key: os.environ/MY_VENDOR_KEY
    api_base: https://api.vendor.example/v1
    use_chat_completions_api: true
  model_info:
    mode: chat
    db_model: true
```

Use this for:

- Real providers with OpenAI-compatible APIs;
- **Local shims** — a tiny HTTP server you write that translates your vendor to
  OpenAI shape (run it on `127.0.0.1:<port>`, point `api_base` at it);
- vLLM / llama.cpp / text-generation-webui servers, etc.

> `use_chat_completions_api: true` forces the chat-completions path. Set it when
> the upstream doesn't implement the newer `/responses` API.

---

## Pattern 2 — Wildcard catalog (any model, resolved live)

For providers with big, changing catalogs. One entry covers everything:

```yaml
- model_name: "openrouter/*"
  litellm_params:
    model: "openrouter/*"
    api_key: os.environ/OPENROUTER_API_KEY
  model_info:
    mode: chat
    db_model: true
```

Clients then call `openrouter/<org>/<model>` — **including models that didn't
exist when you wrote the config**. No registration, no restart.

Trade-offs to know:

- `/v1/models` won't enumerate the wildcard's models (that would flood the list
  with hundreds of entries). The models are callable by name regardless.
- For discovery, write a small sync script that diffs the provider's public
  catalog against a saved snapshot and reports arrivals (see
  `scripts/openrouter_sync.py` for a working example — it needs no auth to read
  the catalog).

---

## Pattern 3 — Fixed cloud gateway

A hosted gateway with a stable list of models — same wildcard trick, plus
`api_base`:

```yaml
- model_name: "ollama/*"
  litellm_params:
    model: "openai/*"                     # passthrough the model id
    api_base: https://ollama.com/v1
    api_key: os.environ/OLLAMA_API_KEY
    use_chat_completions_api: true
```

> **Caution — provider cost maps lie.** LiteLLM's built-in cost map may list
> hundreds of model names for a provider that the provider itself doesn't serve.
> Those names 404 at call time. Before advertising a model list, probe real
> names: `curl … -d '{"model":"provider/<name>", …}'` and keep what answers.
> Some providers also need `name:tag` syntax (`ollama/gpt-oss:20b`, not
> `ollama/gpt-oss`).

---

## Pattern 4 — Local model server

Anything you run yourself is just another lane. Two steps:

**1. Run the server.** Whatever it is — your wrapper script, llama.cpp's server,
vLLM — make it listen on `127.0.0.1:<port>` and speak OpenAI chat completions at
`/v1/chat/completions`. `scripts/laya_server.py` is a complete working example
(stdlib-only HTTP server, ~300 lines) including:

- binding the port **before** loading models, so health checks answer immediately;
- a background preload thread so slow first loads don't look like crashes;
- **SSE streaming support** — clients like Codex/Hermes stream by default, and a
  server that ignores `stream: true` makes the client see an EMPTY response
  (`finish_reason=stop`, no content). Emit at least one content chunk + finish +
  `[DONE]`.

**2. Add the lane:**

```yaml
- model_name: laya                          # the name clients call
  litellm_params:
    model: openai/laya
    api_key: unused-local                   # local servers usually need no key
    api_base: http://127.0.0.1:4030/v1
    use_chat_completions_api: true
```

Multiple checkpoints/aliases = multiple entries pointing at the same port with
different `openai/<alias>` model ids (the wrapper maps alias → checkpoint).

---

## Pattern 5 — Custom provider (request-shape validation)

Some APIs reject normal OpenAI requests until specific fields are present —
required tools, forced streaming, special session headers, unusual auth. LiteLLM
lets you write a `CustomLLM` handler that builds the correct shape and adapts the
response back:

```python
from litellm import CustomLLM

class MyProvider(CustomLLM):
    async def acompletion(self, model, messages, api_base=None, api_key=None,
                          optional_params=None, **kwargs):
        # build the upstream-specific payload, send it, return ModelResponse
        ...
    async def astreaming(self, model, messages, **kwargs):
        # yield OpenAI-style chunks: {"text": ..., "is_finished": ...}
        ...

handler_instance = MyProvider()
```

Register it:

```yaml
litellm_settings:
  custom_provider_map:
    - {"provider": "myprov", "custom_handler": my_provider.handler_instance}
```

Models then use `model: myprov/<upstream-id>`. The handler file (e.g.
`my_provider.py`) must sit next to the config (imports resolve relative to it).

A handler typically covers:

- injecting required request fields (tools, `stream: true`, session headers);
- aggregating a streaming upstream into a non-streaming `ModelResponse` for
  clients that don't stream;
- **translating a non-chat API** — decision-engine endpoints (`/systemone`) that
  take `{state, questions}` instead of messages — see `docs/03-JEV-AND-LAYA.md`.

### Debugging the shape

When an upstream 400s/403s and you can't tell why, capture the exact request the
proxy sends: point the lane's `api_base` at a throwaway logging proxy you control
(or a `nc -l`-style sink), fire one request, and read the captured body. Guessing
from docs wastes hours; the capture takes minutes.

---

## Operational rules for all lanes

- **After adding a lane, update every virtual key** (they have per-key
  allowlists). `POST /key/update`.
- **Restart the proxy once** after editing the YAML (runtime-registered models
  survive restarts; YAML edits need a reload).
- **Probe, then advertise.** Verify each new lane with a real completion before
  listing it in docs/commands.
- **`drop_params: true`** in `litellm_settings` keeps one lane's unsupported
  params from breaking others — keep it on.
