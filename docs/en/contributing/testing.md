# Testing Guide

Reyn's test suite lives in `tests/`. It uses [pytest](https://docs.pytest.org/) and a custom LLM replay mechanism to make tests that exercise LLM-dependent paths deterministic and cost-free in CI.

---

## Running tests

```bash
# All tests (existing + replay)
python -m pytest tests/ -v

# Only replay tests
python -m pytest tests/test_replay_*.py -v
```

---

## Replay tests

### Motivation

OS paths like `skill_router`, `multi-hop relay`, `skill_improver`, and `eval_builder` call the LLM for every phase execution. Without a replay mechanism, tests that cover these paths must either:

- Call the real LLM (expensive, flaky, slow, requires API key), or
- Mock with ad-hoc `unittest.mock.patch` (fragile, detached from actual prompts).

The `@pytest.mark.replay` mechanism offers a third option: **record once, replay forever**. LLM responses are stored in JSONL fixture files and replayed on subsequent test runs without touching the network.

### How it works

1. `litellm.acompletion` (the async LLM boundary used by all Reyn LLM calls) is monkeypatched at test setup time.
2. In **replay mode**, the mock looks up the response by `SHA256(model + canonical_json(messages))`. If the key is found, the saved `ModelResponse` is returned. If not, `MissingFixture` is raised — loudly, so prompt drift is explicit.
3. In **record mode** (`REYN_LLM_RECORD=1`), the real LLM is called and the response is appended to the fixture file.
4. The mock is restored unconditionally after each test — no leakage to adjacent tests.

### Writing a replay test

```python
import pytest
from reyn.llm import call_llm
from reyn.models import ContextFrame, PhaseConstraints, ExecutionState, CandidateOutput
from reyn.testing.replay import REPLAY_DATETIME   # fixed datetime for stable keys

@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase_does_something():
    frame = ContextFrame(
        current_phase="my_phase",
        # ... other fields ...
        current_datetime=REPLAY_DATETIME,   # REQUIRED — see below
    )

    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        call_llm(
            "gemini-2.5-flash-lite",
            frame,
            prompt_cache_enabled=False,
            skill_name="my_skill",
            skill_description="...",
            phase_role="...",
        )
    )

    assert result.data["type"] == "decide"
    # ... more assertions ...
```

**Key rules:**

1. **Always pass `current_datetime=REPLAY_DATETIME`** when constructing `ContextFrame` in replay tests. `ContextFrame.current_datetime` defaults to `datetime.now()`, which changes every run and breaks the SHA-256 key lookup. `REPLAY_DATETIME` is a fixed UTC constant (`2026-01-01T00:00:00+00:00`).

2. **All frame fields must exactly match what the fixture was recorded with.** Even a single-character difference in `instructions`, `candidate_outputs`, or `input_artifact` changes the key. If you intentionally change a frame, re-record the fixture.

3. **Fixture path is relative to `tests/`**, not to the test file. E.g. `"fixtures/llm/skill_router/chitchat.jsonl"`.

### Fixture format

Each JSONL file contains one line per recorded LLM call:

```json
{"key": "<sha256>", "model": "gemini-2.5-flash-lite", "prompt_preview": "...", "response": {...}}
```

- `key` — SHA-256 of `model + canonical_json(messages)`. This is the lookup key.
- `model` — the bare model name (without provider prefix) — for human identification only.
- `prompt_preview` — first 200 characters of the last user-turn message — grep aid, not used for lookup.
- `response` — `litellm.ModelResponse.model_dump()` dict, reconstructed as `ModelResponse` on replay.

### Recording new fixtures

**Scenario: first time you write a replay test** (fixture file does not exist yet)

The conftest detects the missing file and activates record mode automatically:

```bash
# First run — automatically records
python -m pytest tests/test_replay_my_area.py -v
# Fixture is created at tests/fixtures/llm/my_area/my_scenario.jsonl

# Subsequent runs use replay
python -m pytest tests/test_replay_my_area.py -v
```

**Scenario: intentional prompt change** (you changed instructions, candidate schema, etc.)

Delete the affected fixture file and re-record:

```bash
rm tests/fixtures/llm/my_area/my_scenario.jsonl
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
# New fixture written
```

Or force record mode without deleting:

```bash
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
# Appends new entries; existing entries with different keys are preserved
```

**Record mode requires a live LLM backend.** For local development, the project uses a LiteLLM proxy at `localhost:4000` with `OPENAI_API_KEY` set in the shell. See `project_local_env.md` in memory for details.

### Prompt drift detection

A prompt change anywhere in the message chain (system prompt, frame content, prior_attempts, etc.) produces a different SHA-256 key and causes `MissingFixture` to be raised with a diagnostic message:

```
reyn.testing.replay.MissingFixture: No fixture entry for model='gemini-2.5-flash-lite'.
Prompt preview: '{"current_phase": "classify", ...'
Fixture: tests/fixtures/llm/skill_router/chitchat.jsonl
Re-run with REYN_LLM_RECORD=1 to record new fixtures.
```

This is intentional — silent fallthrough (returning a stale fixture that no longer matches the prompt) would mask regressions. When you see `MissingFixture`, either the test frame drifted from the fixture, or you intentionally changed the prompt and need to re-record.

### Monkeypatch leakage

The `_llm_replay` autouse fixture installs and restores `litellm.acompletion` in a `try/finally` block, so it is always restored even if the test raises. Tests that do not have the `@pytest.mark.replay` marker see the real `litellm.acompletion` unmodified. You can verify this with:

```python
def test_no_monkeypatch_leak():
    import litellm
    mod = getattr(litellm.acompletion, "__module__", "") or ""
    assert "reyn" not in mod
```

### Fixture organisation

```
tests/
  fixtures/
    llm/
      skill_router/
        chitchat.jsonl          # direct finish for chitchat
        task_dispatch.jsonl     # classify → match, skills_to_run
      multi_hop/
        agent_delegation.jsonl  # agent A → agent B delegation
        deferred_reply.jsonl    # agent B deferred reply with chain_id
      skill_improver/
        prepare_phase.jsonl     # prepare → copy_to_work
        force_decide.jsonl      # remaining_act_turns=0 path
      eval_builder/
        analyze_skill.jsonl     # basic per-case criteria
        analyze_with_rollback.jsonl  # skill with rollback loop
```

---

## Existing tests (non-replay)

`tests/test_budget_persistent.py` tests the `BudgetTracker` and `BudgetLedger` in isolation without LLM calls. These are synchronous unit tests that do not require `REPLAY_DATETIME` or the replay fixture.

---

## Coverage checklist for new OS features

When adding a new LLM-dependent OS path, add replay tests following this checklist:

- [ ] Happy path with the new path's canonical input
- [ ] At least one corner case (force_decide, error path, boundary condition)
- [ ] `test_wrong_input_raises_missing_fixture` — verifies that prompt drift is caught
- [ ] No `current_datetime=datetime.now()` — always use `REPLAY_DATETIME`
