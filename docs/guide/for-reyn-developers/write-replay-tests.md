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

This tutorial covers **Tier 3a**: single LLM call per test, single phase. If
you are not testing an LLM-dependent path, check the decision flow in
[testing.md](../../deep-dives/contributing/testing.md#decision-flow) first.

---

## Step 1: Record the fixture

### Prerequisites

- LiteLLM proxy running at `localhost:4000` (see `project_local_env.md` in
  memory for the exact setup)
- The model you want to record is available via the proxy

### Write the test first (without a fixture)

Create your test file. Use `@pytest.mark.replay` with a path relative to
`tests/`:

```python
# tests/test_replay_my_area.py

import asyncio
import pytest
from reyn.llm.llm import call_llm
from reyn.schemas.models import ContextFrame, ExecutionState, PhaseConstraints
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"

@pytest.mark.replay("fixtures/llm/my_area/happy_path.jsonl")
def test_my_phase_happy_path():
    """Tier 3a: my_phase transitions to next_phase on valid input."""
    frame = ContextFrame(
        current_phase="my_phase",
        current_phase_role="my_role",
        instructions="... the phase instructions ...",
        candidate_outputs=[],   # fill in from the real skill
        finish_criteria=[],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="standard",
        model_resolved=MODEL,
        input_artifact={"type": "my_input", "data": {"field": "value"}},
        execution=ExecutionState(path=["start → my_phase"], current_visit=1, total_steps=1),
        control_ir_results=[],
        remaining_act_turns=0,
        current_datetime=REPLAY_DATETIME,  # REQUIRED — see below
    )

    result = asyncio.run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="my_skill",
            skill_description="...",
            phase_role="my_role",
        )
    )

    assert result.data["type"] == "decide"
    assert result.data["control"]["type"] == "transition"
    assert result.data["control"]["next_phase"] == "next_phase"
```

**Why `REPLAY_DATETIME` is required**: the `ContextFrame` is serialised into
the prompt, and every field contributes to the SHA-256 fixture key. If you use
`datetime.now()`, the key changes every second and no fixture will ever match.
`REPLAY_DATETIME` is a fixed sentinel (`2026-01-01T00:00:00Z`) that keeps the
key stable across runs.

### Run once to record

When the fixture file does not exist, `conftest.py` switches to record mode
automatically:

```bash
python -m pytest tests/test_replay_my_area.py::test_my_phase_happy_path -v
```

This calls the real LLM, records the response, and writes
`tests/fixtures/llm/my_area/happy_path.jsonl`. The test also passes on this
first run.

After recording, run the test again without a live LLM to confirm it replays
correctly:

```bash
# Stop the proxy, then:
python -m pytest tests/test_replay_my_area.py::test_my_phase_happy_path -v
```

Commit the fixture file alongside the test.

---

## Step 2: Write the test body

### Build the ContextFrame accurately

The fixture key is derived from the exact serialised `(model, messages)` that
`call_llm` sends to litellm. **Every field in `ContextFrame` contributes**,
including:

- `instructions` — must be the real phase instructions, not a placeholder
- `candidate_outputs` — must reflect what the OS would inject at runtime
- `input_artifact` — must match the artifact shape the phase receives

The easiest way to get this right is to load the skill from disk using
`load_dsl_skill` and pull the phase and its schema directly:

```python
from pathlib import Path
from reyn.core.compiler.loader import load_dsl_skill
from reyn.schemas.models import CandidateOutput

_SKILL_PATH = Path(__file__).parent.parent / "src" / "reyn" / "stdlib" / "skills" / "my_skill" / "skill.md"

def _load_skill():
    return load_dsl_skill(_SKILL_PATH)

def _make_frame(skill) -> ContextFrame:
    phase = skill.phases["my_phase"]
    next_phase = skill.phases["next_phase"]
    candidate = CandidateOutput(
        next_phase="next_phase",
        control_type="transition",
        schema_name=next_phase.input_schema_name,
        artifact_schema=next_phase.input_schema,
        description="Transition to next_phase",
    )
    return ContextFrame(
        current_phase="my_phase",
        instructions=phase.instructions,
        candidate_outputs=[candidate],
        # ... other fields ...
        current_datetime=REPLAY_DATETIME,
    )
```

Loading from disk has two benefits: the fixture stays in sync when the skill
evolves (the key changes and `MissingFixture` fires, signalling a re-record),
and the test documents the real call path rather than a synthetic approximation.

### Assert on structure, not wording

Good assertions check the OS-level contract:

```python
# Good — structural contract
assert result.data["control"]["type"] == "transition"
assert result.data["control"]["decision"] == "continue"
assert result.data["control"]["next_phase"] == "run_and_eval"
```

Avoid asserting on free-text fields like `reason.summary` unless the field
content is part of the contract you are pinning. Wording varies across model
versions and causes unnecessary re-records.

One exception: when the test is explicitly verifying that the LLM *read* a
specific field, a `in reason_summary.lower()` assertion is appropriate (see
`test_copy_to_work_validation_judgment.py` for an example).

---

## Step 3: Add a drift detection test

Every Tier 3a area needs one test that asserts `MissingFixture` is raised when
the input does not match any fixture entry. This is the mechanism that catches
accidental prompt drift — if someone changes the phase instructions or the
`ContextFrame` shape, the test fails loudly rather than silently passing with
stale fixture data.

```python
from reyn.testing.replay import MissingFixture

@pytest.mark.replay("fixtures/llm/my_area/happy_path.jsonl")
def test_drift_detection_raises_missing_fixture():
    """Tier 3a drift detection: prompt changes must be reflected in re-recorded fixtures."""
    frame = ContextFrame(
        current_phase="my_phase",
        instructions="intentionally not in the fixture — drift sentinel",
        candidate_outputs=[],
        finish_criteria=[],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="standard",
        model_resolved=MODEL,
        input_artifact={"type": "my_input", "data": {}},
        execution=ExecutionState(path=[], current_visit=1, total_steps=1),
        control_ir_results=[],
        remaining_act_turns=0,
        current_datetime=REPLAY_DATETIME,
    )

    with pytest.raises(MissingFixture):
        asyncio.run(
            call_llm(MODEL, frame, prompt_cache_enabled=False,
                     skill_name="my_skill", skill_description="...", phase_role="my_role")
        )
```

The drift detection test re-uses the same fixture file as the happy-path test.
A frame with different instructions produces a different SHA-256 key, which is
not in the fixture, so `MissingFixture` is raised.

---

## Step 4: Update fixtures after intentional prompt changes

When you intentionally change phase instructions, the fixture key changes.
Replay mode will raise `MissingFixture`. This is expected and correct behavior.

Re-record by deleting the fixture and running with `REYN_LLM_RECORD=1`:

```bash
rm tests/fixtures/llm/my_area/happy_path.jsonl
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
```

Or delete and let conftest auto-detect the missing file:

```bash
rm tests/fixtures/llm/my_area/happy_path.jsonl
python -m pytest tests/test_replay_my_area.py -v
# conftest sees file missing → switches to record mode automatically
```

Commit the new fixture alongside the prompt change. Reviewers can diff the
`prompt_preview` field in the JSONL to see what changed.

> **Warning — `-k` filtered runs exclude replay tests.** If your local test run uses
> `-k some_keyword` that does not match replay test names, replay tests are silently
> skipped and the run appears green. This masks broken fixtures until CI runs the
> full suite. Always run the full replay suite (`pytest tests/test_replay_*.py -v`)
> after any change to phase instructions, tool catalog, or LLM-call boundaries.

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
assert skill_node._cached_frame is not None
```

Assert on the public return value of the function under test.

**Do not use `datetime.now()` in `ContextFrame`.**

```python
# FORBIDDEN — breaks fixture keys
frame = ContextFrame(current_datetime=datetime.now(), ...)

# Required
from reyn.testing.replay import REPLAY_DATETIME
frame = ContextFrame(current_datetime=REPLAY_DATETIME, ...)
```

**Do not write Tier 4 tests.** Common Tier 4 traps:

- Testing that `result.data["reason"]["summary"]` contains a specific
  sentence — this pins LLM output wording, which drifts with every model
  update.
- Testing internal cache state or flag values (`_state_loaded`, `_initialized`).
- Adding a test for "this specific bug we fixed" unless it represents a
  genuine P1–P8 invariant.

---

## Complete example

The following is drawn from the actual `test_copy_to_work_validation_judgment.py`
in the test suite. It tests two LLM judgment cases in the `copy_to_work` phase
of the `skill_improver` stdlib skill.

```python
"""Tier 3a: copy_to_work phase validation judgment behavior.

Two cases are pinned:
  - Case 1 (validation.ok=True):  LLM must transition to run_and_eval.
  - Case 2 (validation.ok=False): LLM must abort.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.compiler.loader import load_dsl_skill
from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"
SKILL_NAME = "skill_improver"
PHASE_ROLE = "workspace_initializer"

_SKILL_PATH = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "skill_improver" / "skill.md"
)


def _make_frame(skill, validation_ok: bool) -> ContextFrame:
    phase = skill.phases["copy_to_work"]
    next_phase = skill.phases["run_and_eval"]
    candidate = CandidateOutput(
        next_phase="run_and_eval",
        control_type="transition",
        schema_name=next_phase.input_schema_name,
        artifact_schema=next_phase.input_schema,
        description="Transition to run_and_eval",
    )
    return ContextFrame(
        current_phase="copy_to_work",
        current_phase_role=PHASE_ROLE,
        instructions=phase.instructions,
        candidate_outputs=[candidate],
        finish_criteria=[],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="standard",
        model_resolved=MODEL,
        input_artifact={
            "type": "improvement_session",
            "data": {
                "target_skill": "direct_llm",
                "validation": {
                    "ok": validation_ok,
                    "files_written": 2 if validation_ok else 0,
                    "files_expected": 2,
                },
            },
        },
        execution=ExecutionState(
            path=["prepare → copy_to_work"], current_visit=1, total_steps=1
        ),
        control_ir_results=[],
        remaining_act_turns=0,
        current_datetime=REPLAY_DATETIME,
    )


@pytest.mark.replay("fixtures/llm/copy_to_work_validation/validation_ok.jsonl")
def test_copy_to_work_transitions_when_validation_ok():
    """Tier 3a (LLM replay): copy_to_work transitions to run_and_eval when validation.ok=True."""
    skill = load_dsl_skill(_SKILL_PATH)
    frame = _make_frame(skill, validation_ok=True)

    result = asyncio.run(
        call_llm(MODEL, frame, prompt_cache_enabled=False,
                 skill_name=SKILL_NAME, skill_description="...", phase_role=PHASE_ROLE)
    )

    ctrl = result.data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "run_and_eval"
    assert ctrl["decision"] == "continue"


@pytest.mark.replay("fixtures/llm/copy_to_work_validation/validation_fail.jsonl")
def test_copy_to_work_aborts_when_validation_fails():
    """Tier 3a (LLM replay): copy_to_work aborts when validation.ok=False."""
    skill = load_dsl_skill(_SKILL_PATH)
    frame = _make_frame(skill, validation_ok=False)

    result = asyncio.run(
        call_llm(MODEL, frame, prompt_cache_enabled=False,
                 skill_name=SKILL_NAME, skill_description="...", phase_role=PHASE_ROLE)
    )

    ctrl = result.data["control"]
    assert ctrl["type"] == "abort"
    assert ctrl["decision"] == "abort"
```

---

## Coverage checklist for a new LLM-dependent path

When adding a new LLM-dependent OS path, verify:

- [ ] One Tier 3a test for the canonical happy path
- [ ] One Tier 3a test for a boundary or error case (optional but recommended)
- [ ] One drift detection test (`MissingFixture` assertion) per fixture file
- [ ] `current_datetime=REPLAY_DATETIME` in every `ContextFrame`
- [ ] Each test docstring starts with `"""Tier 3a: ...`
- [ ] Fixture file committed alongside the test
- [ ] No `MagicMock` / `AsyncMock` / `patch` anywhere in the file
- [ ] If the path also derives from a P1–P8 invariant, add a Tier 2 test for the invariant separately

---

## Reference

- Full LLMReplay API: [docs/reference/testing/replay.md](../../reference/testing/replay.md)
- Full testing policy (Tier definitions, NEVER rules, decision flow): [docs/deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md)
- Live examples in the codebase:
  - `tests/test_copy_to_work_validation_judgment.py` — two judgment cases with `load_dsl_skill`
  - `tests/conftest.py` — `_llm_replay` autouse fixture and mode resolution
