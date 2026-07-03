# `reyn.dev.testing.LLMReplay` API

`LLMReplay` is the core class powering `@pytest.mark.replay`. It monkeypatches `litellm.acompletion` — the async boundary every Reyn LLM call passes through (reached via `reyn.llm.llm.call_llm_tools`, the tool_use wrapper the chat router drives).

```python
from reyn.dev.testing.replay import LLMReplay, MissingFixture
```

---

## `class LLMReplay`

```python
class LLMReplay:
    def __init__(self, fixture_path: Path, mode: Literal["replay", "record"]) -> None: ...
    def install(self) -> None: ...
    def restore(self) -> None: ...
    def flush(self) -> None: ...
```

### Constructor

| Parameter | Type | Description |
|---|---|---|
| `fixture_path` | `Path` | Path to the `.jsonl` fixture file. Does not need to exist when `mode="record"`. |
| `mode` | `"replay"` or `"record"` | Replay mode returns saved responses. Record mode calls the real LLM and writes entries. |

### `install()`

Replaces `litellm.acompletion` with this instance's `_handle` coroutine. Call before the code under test runs. Always pair with `restore()` in a `finally` block.

### `restore()`

Restores the original `litellm.acompletion`. Safe to call even if `install()` was never called.

### `flush()`

Writes pending record-mode entries to `fixture_path`. Appends to the existing file; creates the file and parent directories if they do not exist. No-op in replay mode or when there are no pending entries.

### Context manager

`LLMReplay` supports `with` syntax:

```python
with LLMReplay(fixture_path, mode="replay") as replay:
    result = await call_llm_tools(...)
```

`__exit__` calls `restore()` and, if `mode="record"`, `flush()`.

---

## `class MissingFixture(Exception)`

Raised in replay mode when no fixture entry matches the SHA-256 key for the current `(model, messages, tools, tool_choice)` combination. The error message includes:

- The model name
- A 200-character preview of the last user-turn message
- The fixture file path
- Instructions for re-recording

---

## Key computation

The key depends on whether the call carries tools. `canonical(x)` is
`json.dumps(x, sort_keys=True, ensure_ascii=False)`:

```python
# No tools and no tool_choice (legacy form — preserves pre-tools fixture keys):
key = SHA256(model.encode() + canonical(messages).encode())

# With tools or tool_choice:
key = SHA256(f"{model}|{canonical(messages)}|{canonical(tools)}|{tool_choice or ''}".encode())
```

**Every byte of the serialised inputs contributes to the key** — the full
system prompt, the message list, and (when present) the tool catalog and
`tool_choice`. Changing any of them invalidates the key and causes
`MissingFixture` in replay mode. This is intentional — it makes prompt drift
explicit. Keep test inputs free of volatile values (timestamps, uuids) so the
key stays reproducible.

---

## Fixture format

JSONL file, one JSON object per line:

```json
{
  "key": "<sha256-hex>",
  "model": "gemini-2.5-flash-lite",
  "prompt_preview": "<first 200 chars of last user message>",
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
