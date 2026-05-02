# `reyn.testing.LLMReplay` API

`LLMReplay` is the core class powering `@pytest.mark.replay`. It monkeypatches `litellm.acompletion` at the boundary shared by all Reyn LLM calls (`reyn.llm.llm.call_llm` and `reyn.skill.skill_node_runner._adapt_artifact`).

```python
from reyn.testing.replay import LLMReplay, MissingFixture, REPLAY_DATETIME
```

---

## `REPLAY_DATETIME`

```python
REPLAY_DATETIME: datetime  # datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
```

Fixed UTC datetime to pass as `current_datetime` when constructing `ContextFrame` objects in replay tests. Without this, `ContextFrame.current_datetime` defaults to `datetime.now()`, which changes every run and invalidates the SHA-256 fixture key.

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
    result = asyncio.run(call_llm(...))
```

`__exit__` calls `restore()` and, if `mode="record"`, `flush()`.

---

## `class MissingFixture(Exception)`

Raised in replay mode when no fixture entry matches the SHA-256 key for the current `(model, messages)` combination. The error message includes:

- The model name
- A 200-character preview of the last user-turn message
- The fixture file path
- Instructions for re-recording

---

## Key computation

```python
key = SHA256(model.encode() + canonical_json(messages).encode())
```

Where `canonical_json` is `json.dumps(messages, sort_keys=True, ensure_ascii=False)`.

**Every field in `messages` contributes to the key.** This includes:
- The full system prompt (skill name, description, phase role, project context, agent role)
- The serialised `ContextFrame` (all fields, including `current_datetime`)

Changing any of these invalidates the key and causes `MissingFixture` in replay mode. This is intentional — it makes prompt drift explicit.

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
          "content": "{\"type\": \"decide\", ...}",
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

Tests call `asyncio.get_event_loop().run_until_complete(coro)` to drive async coroutines synchronously, matching the style of the existing `test_budget_persistent.py` test suite.

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
