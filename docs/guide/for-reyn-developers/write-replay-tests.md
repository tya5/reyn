---
type: tutorial
topic: os-development
audience: [human]
---

# Write deterministic tests with LLMReplay

This tutorial shows how to add a Tier 3a test — a test that exercises an
LLM-dependent OS path deterministically, without a live LLM and without
hand-written mocks.

By the end you will have:

- a recorded fixture (`.jsonl` file)
- a passing test that runs identically in CI every time
- a drift detection test that fails loudly when the prompt changes

Full policy rationale is in [docs/deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md).
This page focuses on the mechanics.

---

## Why LLMReplay?

LLM calls are non-deterministic. Two calls with the same input can return
different text. That makes tests that call a real LLM unsuitable for CI: they
are slow, require credentials, and fail intermittently.

The standard workaround is a mock:

```python
# FORBIDDEN — do not do this
from unittest.mock import patch
with patch("litellm.acompletion", return_value={"choices": [...]}):
    ...
```

Mocks bypass the real API surface. When `litellm` changes its signature or
response shape, the mock continues to return the hand-crafted dict, the test
passes, and the breakage reaches production silently.

`LLMReplay` is a **Fake**: it patches `litellm.acompletion` at the same
boundary, but reconstructs a real `litellm.ModelResponse` from a previously
recorded response. Signature drift is caught at invocation (TypeError,
AttributeError) rather than in production.

---

## Tier model — quick summary

| Tier | What it pins | Replay involved? |
|---|---|---|
| 1 — Contract | External boundaries (yaml schema, events payload, DSL contracts) | No |
| 2 — OS invariant | P1–P8 invariants and subsystem contracts | No (or stub callable) |
| 3 — LLM-replay | Behavior of LLM-dependent paths via recorded fixtures | Yes (`LLMReplay`) |
| 4 — Don't write | Everything else | — |

This tutorial covers **Tier 3a**: a single LLM call per test. The LLM tool-call
path is reached through `call_llm_tools` (the tool_use wrapper the chat router
drives). If you are not testing an LLM-dependent path, check the decision flow in
[testing.md](../../deep-dives/contributing/testing.md#decision-flow) first.

---

## Step 1: Record the fixture

### Prerequisites

- LiteLLM proxy running at `localhost:4000` (see `project_local_env.md` in
  memory for the exact setup)
- The model you want to record is available via the proxy

### Write the test first (without a fixture)

Create your test file. Mark the test with `@pytest.mark.replay`, passing a
fixture path relative to `tests/`, and drive the real `call_llm_tools`
boundary. There is no frame or context object to build — you pass the plain
`messages` and `tools` dicts that go to litellm:

```python
# tests/test_replay_my_area.py

import pytest

from reyn.llm.llm import call_llm_tools

MODEL = "gemini-2.5-flash-lite"   # bare name — the proxy strips any prefix on
                                  # record, so the replay key matches
MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "run a skill",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


@pytest.mark.replay("fixtures/llm/my_area/text_only.jsonl")
@pytest.mark.asyncio
async def test_my_call_returns_text_when_no_tool_calls():
    """Tier 3a: when the LLM returns plain text, result.content reflects it."""
    result = await call_llm_tools(model=MODEL, messages=MESSAGES, tools=TOOLS)

    assert isinstance(result.content, str)
    assert len(result.content) > 0
    assert result.tool_calls == []
```

### Run once to record

When the fixture file does not exist, `conftest.py` switches to record mode
automatically:

```bash
python -m pytest tests/test_replay_my_area.py::test_my_call_returns_text_when_no_tool_calls -v
```

This calls the real LLM, records the response, and writes
`tests/fixtures/llm/my_area/text_only.jsonl`. The test also passes on this
first run.

After recording, run the test again without a live LLM to confirm it replays
correctly:

```bash
# Stop the proxy, then:
python -m pytest tests/test_replay_my_area.py::test_my_call_returns_text_when_no_tool_calls -v
```

Commit the fixture file alongside the test.

---

## Step 2: Write the test body

### Keep the inputs deterministic

The fixture key is the SHA-256 of the exact `(model, messages, tools,
tool_choice)` that `call_llm_tools` sends to litellm. **Every byte of the
serialised inputs contributes to the key.** Two consequences:

- Build `messages` and `tools` from stable, literal values. Do **not** inject
  volatile data (timestamps, UUIDs, `datetime.now()`, random ids) into the
  prompt — the key would change every run and no fixture would ever match.
- Because the key is a pure function of the inputs, two tests that pass the
  *same* `(model, messages, tools, tool_choice)` share one fixture file. Reuse
  a fixture across tests that exercise the same call.

### Assert on structure, not wording

`call_llm_tools` returns a result with a `.content` string and a normalized
`.tool_calls` list (plain dicts, not litellm internals). Good assertions check
that shape, not the model's free text:

```python
@pytest.mark.replay("fixtures/llm/my_area/tool_call.jsonl")
@pytest.mark.asyncio
async def test_tool_calls_are_normalized():
    """Tier 3a: tool_calls are normalized to plain dicts."""
    messages = [{"role": "user", "content": "call the run_skill tool with skill=hello"}]
    result = await call_llm_tools(
        model=MODEL, messages=messages, tools=TOOLS, tool_choice="required"
    )

    assert len(result.tool_calls) >= 1
    tc = result.tool_calls[0]
    assert isinstance(tc, dict)                       # not a litellm object
    assert tc["type"] == "function"
    assert isinstance(tc["function"]["arguments"], str)   # JSON string, not dict
```

Avoid asserting on free-text `.content` wording unless the exact content is the
contract you are pinning. Wording varies across model versions and causes
unnecessary re-records.

---

## Step 3: Add a drift detection test

`LLMReplay` raises `MissingFixture` in replay mode when the call's key matches
no fixture entry. That is the mechanism that catches accidental prompt drift —
if someone changes the messages or tool catalog the OS sends, the key changes,
no fixture matches, and the test fails loudly rather than passing on stale data.

```python
from reyn.dev.testing.replay import MissingFixture


@pytest.mark.replay("fixtures/llm/my_area/text_only.jsonl")
@pytest.mark.asyncio
async def test_drift_detection_raises_missing_fixture():
    """Tier 3a drift detection: input changes must be reflected in re-recorded fixtures."""
    drift_messages = [{"role": "user", "content": "not in the fixture — drift sentinel"}]
    with pytest.raises(MissingFixture):
        await call_llm_tools(model=MODEL, messages=drift_messages, tools=TOOLS)
```

The drift test re-uses the same fixture file as the happy path. Different
`messages` produce a different SHA-256 key, which is not in the fixture, so
`MissingFixture` is raised.

---

## Step 4: Update fixtures after intentional input changes

When you intentionally change the messages or tool catalog, the fixture key
changes and replay mode raises `MissingFixture`. This is expected and correct.

Re-record by deleting the fixture and running with `REYN_LLM_RECORD=1`:

```bash
rm tests/fixtures/llm/my_area/text_only.jsonl
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
```

Or delete and let conftest auto-detect the missing file:

```bash
rm tests/fixtures/llm/my_area/text_only.jsonl
python -m pytest tests/test_replay_my_area.py -v
# conftest sees file missing → switches to record mode automatically
```

Commit the new fixture alongside the change. Reviewers can diff the
`prompt_preview` field in the JSONL to see what changed.

> **Warning — `-k` filtered runs exclude replay tests.** If your local test run uses
> `-k some_keyword` that does not match replay test names, replay tests are silently
> skipped and the run appears green. This masks broken fixtures until CI runs the
> full suite. Always run the full replay suite after any change to the tool catalog
> or LLM-call boundaries.

---

## NEVER rules for replay tests

These are absolute. Violations are rejected in PR review.

**Do not mock with `MagicMock` / `AsyncMock` / `patch`.**

```python
# FORBIDDEN
from unittest.mock import patch, AsyncMock
with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
    mock_llm.return_value = {"choices": [...]}
    ...
```

Use `@pytest.mark.replay` with a real recorded fixture instead.

**Do not assert on private state.**

```python
# FORBIDDEN — Tier 4
assert runtime._last_llm_response["id"] == "chatcmpl-abc"
```

Assert on the public return value of the function under test
(`result.content`, `result.tool_calls`).

**Do not inject volatile values into the prompt.**

```python
# FORBIDDEN — breaks fixture keys (key changes every run)
messages = [{"role": "user", "content": f"now is {datetime.now()}"}]
```

Build `messages` and `tools` from stable literals so the SHA-256 key is
reproducible.

**Do not write Tier 4 tests.** Common Tier 4 traps:

- Asserting that `.content` contains a specific sentence — this pins LLM output
  wording, which drifts with every model update.
- Testing internal cache state or flag values (`_state_loaded`, `_initialized`).
- Adding a test for "this specific bug we fixed" unless it represents a
  genuine P1–P8 invariant.

---

## Coverage checklist for a new LLM-dependent path

When adding a new LLM-dependent OS path, verify:

- [ ] One Tier 3a test for the canonical happy path
- [ ] One Tier 3a test for a boundary or error case (optional but recommended)
- [ ] One drift detection test (`MissingFixture` assertion) per fixture file
- [ ] `messages` / `tools` built from stable literals (no volatile values)
- [ ] Each test docstring starts with `"""Tier 3a: ...`
- [ ] Fixture file committed alongside the test
- [ ] No `MagicMock` / `AsyncMock` / `patch` anywhere in the file
- [ ] If the path also derives from a P1–P8 invariant, add a Tier 2 test for the invariant separately

---

## Reference

- Full LLMReplay API: [docs/reference/testing/replay.md](../../reference/testing/replay.md)
- Full testing policy (Tier definitions, NEVER rules, decision flow): [docs/deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md)
- Live examples in the codebase:
  - `tests/test_llm_tools.py` — the canonical `@pytest.mark.replay` tests for `call_llm_tools`
  - `tests/conftest.py` — the `_llm_replay` autouse fixture and record/replay mode resolution
