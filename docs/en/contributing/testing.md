# Testing Policy

Reyn aims for **predictability over autonomy** (see [Project vision](../concepts/principles.md)). Its test suite reflects that aim: **tests must guard the invariants that protect the OS, and must not become a tax on future evolution.**

This document is the policy. New tests should pass through the [decision flow](#decision-flow) before being written, and existing tests not consistent with the policy will be refactored or removed when next touched.

---

## Core principle

> A good test is judged by what it *signals when it breaks*. "Hard to break" is not a virtue — that property belongs to the design (P1–P8) and to the OS, not to the test.

If a test cannot articulate which contract or invariant it protects, it is implementation pinning in disguise. Implementation pinning is the most common cause of test rot: the test breaks every time the implementation evolves, the author updates it without re-evaluating its purpose, and the suite slowly becomes a friction layer rather than a feedback layer.

---

## Tier model

Tests in `tests/` belong to exactly one tier. The tier determines what the test pins, who the audience is, and when the test should change.

### Tier 1 — Contract

**Pins**: external boundaries that users / OSS contributors / integration scripts depend on.

- `reyn.yaml` schema (required fields, types, error cases)
- Events JSONL payload schemas (audit and replay tooling depend on these)
- DSL contracts: required sections of `skill.md`, `phase.md`, `artifact.yaml`
- Public Python API surface re-exported from each cluster's `__init__.py`

**Granularity**: schema-level. Specific wording is not pinned, except for error message tokens that users grep for (e.g. an exception class name, a config key name).

**Pending**: CLI output formatting is **not** Tier 1 in the current revision. CLI UX is being reworked; CLI output contracts will be added after the redesign.

### Tier 2 — OS invariant

**Pins**: P1–P8 derived invariants of the OS itself.

- LLM output contract (`type=transition` ⇒ `next_phase` non-null; `type=finish` ⇒ `next_phase` null)
- **P1**: a phase that includes its own output schema is rejected by the OS
- **P5**: data passed between phases outside the workspace channel is not honored as input to the next phase
- **P6**: state mutations that bypass the events log are detected (= every state mutation produces an event)

**Granularity**: invariants. The test must fail when the invariant is violated, regardless of *how* it was violated.

**Target count**: 1–2 cases per principle, total 5–10. More than that suggests the policy is being used as a place to dump implementation tests.

### Tier 3 — Behavior tests (deterministic, fake LLM)

**Pins**: behavior of LLM-dependent OS paths, exercised through a *fake* LLM (not a mock) at the `litellm.acompletion` boundary. **Mocks are forbidden — see [Mock vs Fake](#mock-vs-fake) below.**

#### Tier 3a — Single-call replay (current scope)

One LLM call per test, one phase. Canonical example: "Given this `ContextFrame`, the router classifies the intent as X". Drift detection is mandatory: each area also has a test that intentionally diverges and asserts `MissingFixture` is raised.

Areas covered today:
- `skill_router` — intent classification (1–2 typical, 1 drift)
- `multi_hop` — chain_id propagation, deferred reply (1 typical, 1 drift)
- `skill_improver` — temp-copy workflow + force_decide (1 typical, 1 drift)
- `eval_builder` — per-case criteria, rollback-loop case (1 typical, 1 drift)

**Target count**: 6–8 cases total across all areas (cap is 4 areas × 2 cases). 12+ cases is a sign of redundant corner-case coverage that belongs in Tier 4 (don't write).

#### Tier 3b — End-to-end scenario replay (deferred)

Multi-phase sessions, asserting on final state of workspace + events store. Currently **out of scope**: depends on the CLI / `ChatSession` driver, which is being reworked. To be added after the CLI redesign.

### Tier 4 — Don't write

Tests that fall in this list are **not** added to the suite, even when they would technically pass:

- **Direct assertions on private state** (`tracker._daily_tokens == 100`). Use `snapshot()` / public API instead.
- **Algorithm pinning** (sort order, dict iteration order, internal cache structure)
- **Per-commit regression duplicates**. The fix is the commit; the description in the PR is the record. Don't add a test for "this specific bug" unless it represents a genuine invariant that should hold forever.
- **LLM output quality / semantic correctness** ("is this answer useful?"). This belongs to the `eval` skill (LLM-as-judge), not the test suite — see [Out of policy](#out-of-policy).
- **Cosmetic format pins** (whitespace, punctuation, line counts, colour codes)
- **Snapshot / golden file tests** — see [Why no snapshot tests](#why-no-snapshot-tests). Narrow exception in the [Annex](#annex-scaffolding-tests).
- **`unittest.mock` patches of `litellm`** — use the [Fake](#mock-vs-fake) (`LLMReplay`) path instead.
- **Coverage targets** (e.g. "≥ 80% line coverage"). Coverage is a side-effect, not a goal. We do not gate PRs on it.
- **TDD by default**. Test-first is appropriate for Tier 2 invariants (where the contract is clear before the implementation). For feature work, "make it work, then guard it" is preferred — premature tests freeze designs that haven't been validated.

---

## Decision flow

Before writing a test, answer these questions:

```
Q1. If this breaks, who notices?
  A. External user / integrator              → Tier 1 (CLI output is currently deferred)
  B. The OS itself (an invariant fails)      → Tier 2
  C. A single LLM call drifts                → Tier 3a
  D. A whole session drifts                  → Tier 3b (deferred — wait for CLI redesign)
  E. Only the author of this commit          → don't write — PR description is enough

Q2. Will this become a friction in future work?
  - Pins a shape that skill changes will touch          → don't write
  - Pins a private name that refactor will rename       → don't write
  - Pins behavior the DSL is expected to extend         → don't write

Q3. At what level does it pin?
  - Public contract / OS invariant level                → write
  - Implementation level                                → don't write

Q4. Are you measuring LLM semantic quality?
  → Out of test-suite scope. Use the `eval` skill (LLM-as-judge).
    Reference: Anthropic's "regression eval" vs "capability eval" split.
```

When a test cannot be placed cleanly in Tier 1–3, it almost always belongs in Tier 4.

---

## Mock vs Fake

LLM-dependent tests **must** use the Fake (`LLMReplay`). Mocks are forbidden.

### Why

A mock replaces the function with a hand-written stub:

```python
# FORBIDDEN
from unittest.mock import patch
with patch("litellm.acompletion", return_value=hand_built_dict):
    ...
```

This bypasses the real API contract. When `litellm` changes its signature or response shape (e.g. when LangChain renamed `__call__` to `invoke()`, mocked tests across the ecosystem continued to pass while production was broken — see Lincoln Loop, "Avoiding Mocks: Testing LLM Applications with LangChain in Django"), mocked tests do not detect it.

A fake routes through the real API surface. `LLMReplay` patches `litellm.acompletion`, but reconstructs a real `litellm.ModelResponse` from recorded data. Signature drift is detected at the call site (TypeError, AttributeError) or at lookup time (`MissingFixture`).

### How

```python
@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase():
    from reyn.testing.replay import REPLAY_DATETIME
    frame = ContextFrame(
        # ...
        current_datetime=REPLAY_DATETIME,  # required for stable keys
    )
    response = await call_llm(model, frame, ...)
    assert response.data["type"] == "decide"
```

See [How to write a replay test](#how-to-write-a-replay-test) below for the full setup.

---

## Why no snapshot tests

Snapshot tests pin the structural output of a phase / artifact / final result, then diff future runs against the snapshot. We **do not adopt them**. Reasons:

1. **They contradict P1.** Phase declares only `input_schema` and instructions; output shape is determined externally by the next phase's `input_schema` or by `final_output_schema`. A snapshot freezes that output shape inside a test, in tension with P1.
2. **Skill evolution breaks them.** Every skill modification touches artifacts, so snapshots are updated routinely. Routine snapshot updates devolve into "looks plausible, accept" — the snapshot stops being a guard.
3. **The diff review becomes vibe-checking.** Without an articulated invariant, "snapshot updated" reviews degrade into eyeballing. There is no principled way to tell "expected change" from "regression".
4. **Tier 2 (OS invariant) is the better tool.** What the snapshot tries to protect is usually some invariant about the LLM output structure or workspace state. Encode that invariant directly.

Industry literature aligns: see Coulman, *Snapshot Testing: Use With Care* (2016); Hughes, *Why Snapshot Testing Sucks*; the meta-analysis in *Snapshot Testing in Practice: Benefits and Drawbacks* (Science of Computer Programming, 2024).

A narrow exception exists in the [Annex](#annex-scaffolding-tests) for legacy refactor characterization, following Coulman's original framing.

---

## Annex: Scaffolding tests

This is the only place tests with bounded life are allowed. **Scaffolding is not a Tier** — it is intentionally framed as a special-case exception so the `tests/` suite as a whole stays principled.

### When

You are about to do a substantial refactor or migration of an existing area, and you want to catch unintended behavior changes during the work. A scaffolding test pins the current behavior, lives only as long as the refactor, and is removed when the refactor is done.

### Required metadata

```python
# scaffold: triggered_by="When BudgetLedger is replaced with a different backing store"
# scaffold: removed_by="The PR that lands the new backing store"
def test_ledger_jsonl_format_during_migration():
    ...
```

The trigger must be **observable**. "When this code path is rewritten" is fine; "when we have time" or "after Q4" is not.

### Removal hygiene

The PR that fires the trigger event **must also remove the scaffolding tests in the same PR**. PR review checks for this.

### Physical isolation

Scaffolding tests live in `tests/scaffold/`. Files under that directory are scanned during PR review for stale triggers (whose triggering event has already happened).

### Snapshot test exception

A snapshot test is permitted **only** as scaffolding for legacy refactor (Coulman's "characterization test" use case). It must:
- live in `tests/scaffold/`,
- have a concrete `triggered_by` (the refactor PR or release),
- be removed when the refactor lands.

This is the only sanctioned use of snapshot tests in the codebase.

---

## Out of policy

These belong outside the test suite:

- **LLM output semantic quality.** "Is this response actually useful?" is the `eval` skill's job (LLM-as-judge). The test suite asks "did the structure stay correct" — Anthropic calls this *regression eval*. Quality is *capability eval* and lives elsewhere.
- **Model-vs-model benchmarks** (gemini vs claude vs gpt). Use the `eval` skill or a dedicated benchmark tool.
- **Production traffic monitoring / alerts.** Use `events.jsonl` plus external monitoring; this is operational infrastructure, not a test.

---

## How to write a replay test

> Reference for Tier 3a tests, which are the most common contribution shape.

### Boilerplate

```python
import pytest
import asyncio
from reyn.llm.llm import call_llm
from reyn.schemas.models import ContextFrame
from reyn.testing.replay import REPLAY_DATETIME


@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase_classifies_as_x():
    """Tier 3a: skill_router classifies a chitchat input as finish."""
    frame = ContextFrame(
        current_phase="classify",
        # ... other fields ...
        current_datetime=REPLAY_DATETIME,   # REQUIRED
    )

    result = asyncio.get_event_loop().run_until_complete(
        call_llm(
            model="gemini-2.5-flash-lite",
            frame=frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            phase_role="chat_router",
        )
    )

    assert result.data["type"] == "decide"
    assert result.data["control"]["decision"] == "finish"
```

### Fixture path

Path is relative to `tests/`. E.g. `"fixtures/llm/skill_router/chitchat.jsonl"`.

### Recording fixtures

**First time** (fixture file does not exist): conftest detects this and switches to record mode automatically. You need a live LLM available (LiteLLM proxy at `localhost:4000` for local dev — see `project_local_env.md` in memory).

```bash
python -m pytest tests/test_replay_my_area.py -v
# Fixture written to tests/fixtures/llm/my_area/my_scenario.jsonl
```

**After intentional prompt drift**: delete the fixture and re-record:

```bash
rm tests/fixtures/llm/my_area/my_scenario.jsonl
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
```

### Drift detection — required for each area

Each Tier 3a area has one test that intentionally constructs a frame the fixture does not cover, asserting that `MissingFixture` is raised. This is the mechanism that catches accidental prompt drift.

```python
@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_wrong_input_raises_missing_fixture():
    """Tier 3a drift detection: changes to instructions / candidate_outputs
    must be reflected in re-recorded fixtures, otherwise the test fails loudly."""
    frame = ContextFrame(
        current_phase="classify",
        instructions="this is intentionally not in the fixture",
        current_datetime=REPLAY_DATETIME,
    )
    from reyn.testing.replay import MissingFixture
    with pytest.raises(MissingFixture):
        asyncio.get_event_loop().run_until_complete(call_llm(...))
```

### Fixture format

JSONL, one record per line:

```json
{"key": "<sha256>", "model": "gemini-2.5-flash-lite", "prompt_preview": "...", "response": {...}}
```

- `key` — `SHA256(model + canonical_json(messages))`
- `prompt_preview` — first 200 characters of the last message (grep aid)
- `response` — `litellm.ModelResponse.model_dump()`, reconstructed on replay

### Monkeypatch lifecycle

`tests/conftest.py` installs `LLMReplay` for tests with `@pytest.mark.replay` and restores in `try/finally`. Tests without the marker see real `litellm.acompletion`. Verified by `test_no_monkeypatch_leak` in `tests/test_replay_skill_router.py`.

---

## Running tests

```bash
# All tests
python -m pytest tests/ -v

# Only replay tests
python -m pytest tests/test_replay_*.py -v

# Only OS invariant tests (Tier 2)
python -m pytest tests/test_os_invariants.py -v

# Force record mode (live LLM required)
REYN_LLM_RECORD=1 python -m pytest tests/ -v
```

---

## Coverage checklist for a new OS feature

When adding a new LLM-dependent OS path:

- [ ] One Tier 3a test for the canonical happy path
- [ ] One Tier 3a test for one corner case (force_decide, error path, boundary)
- [ ] One drift detection test (`MissingFixture` assertion)
- [ ] If the feature derives from a P1–P8 invariant, add a Tier 2 test for it
- [ ] If the feature changes a public contract (yaml schema, events payload, DSL section), update / add a Tier 1 test
- [ ] Verify no `current_datetime=datetime.now()` — always `REPLAY_DATETIME`
- [ ] Each test has a one-line docstring naming its tier (e.g. `"""Tier 3a: ..."""`)
