# `reyn.dev.testing.LLMReplay` API

`LLMReplay` is the core class powering `@pytest.mark.replay`. It monkeypatches `litellm.acompletion` — the async boundary every Reyn LLM call passes through (reached via `reyn.llm.llm.call_llm_tools`, the tool_use wrapper the chat router drives) — and, since #3451, `litellm.aembedding` alongside it, so both async boundaries reyn's own source calls are intercepted by one instance.

```python
from reyn.dev.testing.replay import LLMReplay, MissingFixture, PreconditionMismatch
```

---

## `class LLMReplay`

```python
class LLMReplay:
    def __init__(
        self,
        fixture_path: Path,
        mode: Literal["replay", "record"],
        preconditions: Sequence[EnvironmentPrecondition] | None = None,
    ) -> None: ...
    def install(self) -> None: ...
    def restore(self) -> None: ...
    def flush(self) -> None: ...
```

### Constructor

| Parameter | Type | Description |
|---|---|---|
| `fixture_path` | `Path` | Path to the `.jsonl` fixture file. Does not need to exist when `mode="record"`. |
| `mode` | `"replay"` or `"record"` | Replay mode returns saved responses. Record mode calls the real LLM and writes entries. |
| `preconditions` | `Sequence[EnvironmentPrecondition] \| None` | Environment-derived values kept OUT of the key and checked instead (#3473 — see [Environment preconditions](#environment-preconditions)). `None` uses `replay_preconditions.default_preconditions()`. `()` disables both the scrub and the check, reproducing the pre-#3473 key exactly. |

### `install()`

Replaces `litellm.acompletion` and `litellm.aembedding` with this instance's handler coroutines. Call before the code under test runs. Always pair with `restore()` in a `finally` block.

In **replay** mode this first *injects* every environment snapshot the fixture carries (#3473), so the run's environment is the fixture's environment before the first call is made.

### `restore()`

Restores the original `litellm.acompletion` / `litellm.aembedding`. Safe to call even if `install()` was never called.

### `flush()`

Writes pending record-mode entries to `fixture_path`. Appends to the existing file; creates the file and parent directories if they do not exist. No-op in replay mode.

In **record** mode it additionally asks each precondition to *capture* the live environment and writes one `"environment"` line per captured snapshot (#3473) — at the end of recording, when whatever populates that environment has had the whole run to do so. A replay-mode `flush()` captures nothing: appending this machine's environment to a committed fixture is the opposite of the guarantee.

### Context manager

`LLMReplay` supports `with` syntax:

```python
with LLMReplay(fixture_path, mode="replay") as replay:
    result = await call_llm_tools(...)
```

`__exit__` calls `restore()` and, if `mode="record"`, `flush()`.

---

## `class MissingFixture(Exception)`

Raised in replay mode when no fixture entry matches the SHA-256 key for the current scenario — `(model, messages, scrub(tools), tool_choice)`, see [Key computation](#key-computation). The error message includes:

- The model name
- A 200-character preview of the last user-turn message
- The fixture file path
- **Key-component attribution** (#3473) — which of `model` / `messages` /
  `tools` / `tool_choice` differs from the nearest recorded entry, down to the
  message index and the tool name, with the components that MATCH reported as
  such so they can be ruled out. Entries recorded before #3473 carry no
  fingerprint; the report then says attribution is unavailable rather than
  claiming everything matched.
- This run's observed environment preconditions
- Instructions for re-recording

## `class PreconditionMismatch(MissingFixture)`

Raised (#3473) when a fixture entry DOES match the scenario but the
environment it was captured under differs from this run's — a genuinely
different diagnosis from "never recorded", reported separately and naming the
difference. It subclasses `MissingFixture` so existing
`except MissingFixture` handlers keep working.

---

## Key computation

The key is the **scenario**. `canonical(x)` is
`json.dumps(x, sort_keys=True, ensure_ascii=False)`, and `scrub(tools)` is
`tools` with every registered environment precondition's imprint removed
(#3473 — see the next section; a no-op where no imprint is present, so keys
recorded on a machine without that environment are unchanged):

```python
# No tools and no tool_choice (legacy form — preserves pre-tools fixture keys):
key = SHA256(model.encode() + canonical(messages).encode())

# With tools or tool_choice:
key = SHA256(f"{model}|{canonical(messages)}|{canonical(scrub(tools))}|{tool_choice or ''}".encode())
```

Every byte of the serialised **scenario** contributes to the key — the full
system prompt, the message list, and (when present) `tool_choice` and the
scenario part of the tool catalog. Changing any of them invalidates the key
and causes `MissingFixture` in replay mode. This is intentional — it makes
prompt drift explicit. Keep test inputs free of volatile values (timestamps,
uuids) so the key stays reproducible.

What does **not** contribute is the environment-derived part of the payload.
That is not an oversight to be "fixed" by folding it back in — see below for
why, and for what replaces it.

---

## Environment preconditions

**A replay key contains the SCENARIO; the ENVIRONMENT is not a key component
but a checked PRECONDITION** (#3473). Owned by
`reyn.dev.testing.replay_preconditions`.

Some of what reaches the wire describes the MACHINE rather than the
conversation. The canonical case is the MCP tool catalog: `RouterHostAdapter`
probes each configured MCP server under a deadline and the answer becomes the
`server` / `mcp_tool_name` enums of the MCP tool schemas — so the same
conversation can send a different `tools=` payload depending on whether a
probe answered in time.

Hashing such a value **into** the key makes an environment wobble
indistinguishable from a different conversation: the report is
`MissingFixture` and cannot say why. Dropping it from the key and doing
nothing else is worse — the fixture would then be replayed under tooling it
was never recorded against, silently. So an `EnvironmentPrecondition` declares
these operations over a typed `ReplayRequest` (the whole key input, not only
`tools`) — all six are abstract, none optional:

| Operation | Role |
|---|---|
| `scrub` | Remove the imprint from the key input. Must be a no-op where the imprint is absent, so pre-existing fixtures keep byte-identical keys. |
| `observe` | Read the same imprint back out. Recorded per fixture entry and compared at replay. |
| `absent_value` | The `observe` value meaning "this environment left no imprint" — what a pre-#3473 entry is allowed to be served under. |
| `capture` | Snapshot the live environment at the end of recording, into the fixture. `None` = nothing to record. |
| `inject` | Re-establish that snapshot before replay — a direct write, never a sleep / longer deadline / retry, which only widen the window instead of removing the timing dependence. |
| `describe_mismatch` | Render the difference between the captured and observed imprints as a report that NAMES it. |

`scrub` and `observe` are deliberately the same projection: **nothing can
leave the key without being checked.** A mismatch raises
`PreconditionMismatch` naming the difference.

`MCPCatalogPrecondition` is the shipped instance. It injects into
`<state_dir>/mcp_tools_cache.json` — the persistent cache
`ensure_mcp_tools_cached` warm-starts from before probing anything, written
through the production writer (the same file `reyn mcp refresh` produces), so
an already-answered server is never probed and the deadline is off the replay
path entirely. `state_dir` defaults to `RouterHostAdapter`'s own default,
`.reyn/state` resolved against the current working directory.

To cover a new environment-derived value, implement the protocol and add it to
`default_preconditions()`; the fixture format, the mismatch report and the
injection step already carry it.

---

## Fixture format

JSONL file, one JSON object per line. Lines are routed by `kind`
(absent/`"completion"` = an `acompletion` call; `"embedding"` = an
`aembedding` call, #3451; `"environment"` = a captured environment snapshot,
#3473):

```json
{
  "key": "<sha256-hex>",
  "kind": "completion",
  "model": "gemini-2.5-flash-lite",
  "prompt_preview": "<first 200 chars of last user message>",
  "preconditions": {"mcp_catalog": {"servers": ["…"], "tools": ["…"]}},
  "key_components": {
    "model": "gemini-2.5-flash-lite",
    "tool_choice": "",
    "messages": ["user:<digest>", "…"],
    "tools": {"call_mcp_tool": "<digest>", "…": "…"}
  },
  "response": {
    "id": "chatcmpl-...",
    "created": 1234567890,
    "model": "gemini-2.5-flash-lite",
    "object": "chat.completion",
    "system_fingerprint": null,
    "choices": [
      {
        "finish_reason": "stop",
        "index": 0,
        "message": {
          "content": "Sure — here is the result ...",
          "role": "assistant",
          "tool_calls": null,
          "function_call": null
        }
      }
    ],
    "usage": {
      "completion_tokens": 40,
      "prompt_tokens": 80,
      "total_tokens": 120,
      "completion_tokens_details": null,
      "prompt_tokens_details": null
    }
  }
}
```

The `response` dict is the output of `litellm.ModelResponse.model_dump()`. On replay, it is reconstructed via `litellm.ModelResponse(**response)`.

`preconditions` and `key_components` (#3473) are what the key deliberately
does not carry:

- `preconditions` — `{precondition name: observed imprint}`, the
  environment values scrubbed out of that call's key, recorded so replay can
  CHECK them. An entry with no `preconditions` field predates #3473 and is
  served only when this run's imprint is empty (there, the key is identical to
  the one it was recorded under); under a non-empty environment it is refused,
  because nothing exists to check it against.
- `key_components` — the per-component fingerprint a later miss is attributed
  against. One-way digests: enough to say WHICH component moved, never enough
  to reconstruct the payload.

An `"environment"` line carries one precondition's captured snapshot and has
no `key`:

```json
{"kind": "environment", "name": "mcp_catalog", "value": {"reyn_chunker": [{"name": "chunk"}]}}
```

---

## Sensitive data

`prompt_preview` is capped at 200 characters and is present only for human identification (grep, debugging). Reyn never injects API keys or auth tokens into the `messages` list — those are read from environment variables by litellm internally — so fixtures do not contain credentials.

---

## Async vs sync

Reyn uses `litellm.acompletion` (async) for all LLM calls. `LLMReplay` monkeypatches the async variant only. `litellm.completion` (sync) is not patched and is never used by Reyn's production paths.

Replay tests are `async def` and marked `@pytest.mark.asyncio`, awaiting the coroutine under test directly (`result = await call_llm_tools(...)`).

---

## Integration with pytest

`@pytest.mark.replay` is registered in `tests/conftest.py`:

```python
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "replay(fixture): monkeypatch litellm.acompletion with a JSONL fixture.",
    )
```

The `_llm_replay` autouse fixture detects the marker, resolves the fixture path relative to `tests/`, determines the mode, and wraps the test body:

```python
@pytest.fixture(autouse=True)
def _llm_replay(request):
    marker = request.node.get_closest_marker("replay")
    if marker is None:
        yield
        return
    # resolve path, determine mode, install, yield, restore, flush
```

Mode resolution:

| Condition | Mode |
|---|---|
| `REYN_LLM_RECORD=1` in env | `"record"` |
| Fixture file does not exist | `"record"` (first-run bootstrap) |
| Otherwise | `"replay"` |
