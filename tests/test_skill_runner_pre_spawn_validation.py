"""Tier 2: OS invariant tests for SkillRunner pre-spawn input_schema
validation.

Asserts that SkillRunner.spawn() validates ``input`` against the loaded
skill's entry_input_schema BEFORE creating the asyncio task, and that the
structured error returned propagates through spawn_for_router to the
router LLM's tool_result.

Why this matters: post-spawn validation arrives at the router LLM as an
async [task_completed] kind=skill status=error message that's temporally
separated from the originating invoke_action call. Weak-tier LLMs
struggle to correlate the failure back to the original args + retry.
Pre-spawn validation keeps the error in the same tool_result round-trip
as the wrong args, so the LLM can react with full local context.

No mocks — uses the existing _make_runner fixture pattern that
monkeypatches resolve_skill_path + load_dsl_skill with real-ish stubs.
"""
import asyncio
from types import SimpleNamespace

import pytest

# Re-use the helpers from the sibling invariants test module so
# fixtures stay coherent.
from test_skill_runner_invariants import _make_runner


def _make_stub_skill(input_schema=None):
    """Stub skill object exposing only the attributes spawn() touches.

    Pre-spawn validation reads ``skill.entry_input_schema``; nothing
    else is needed for the validation branch tests.
    """
    return SimpleNamespace(entry_input_schema=input_schema)


# ---------------------------------------------------------------------------
# Tier 2: invariant 1 — no schema → spawn proceeds (legacy behavior preserved)
# ---------------------------------------------------------------------------


def test_spawn_no_schema_proceeds(tmp_path, monkeypatch):
    """Tier 2: when the loaded skill has no entry_input_schema, spawn()
    skips validation and proceeds to create the asyncio task (= legacy
    behavior preserved for skills without declared schemas)."""
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "skill_no_schema"
    dummy_dir.mkdir()
    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(
        sr_mod, "load_dsl_skill",
        lambda path, *, skill_root: _make_stub_skill(input_schema=None),
    )

    block = asyncio.Event()
    runner, _events, _outbox, _completed = _make_runner(block_on=block)

    async def _run():
        result = await runner.spawn({"skill": "noschema", "input": {"any": "shape"}})
        assert result is None, f"expected None on success, got {result!r}"
        assert len(runner.running_names()) == 1, "task should be running"
        block.set()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tier 2: invariant 2 — schema + valid input → spawn proceeds
# ---------------------------------------------------------------------------


def test_spawn_valid_input_proceeds(tmp_path, monkeypatch):
    """Tier 2: input that matches the entry_input_schema passes through
    pre-spawn validation; the asyncio task is created."""
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "skill_with_schema"
    dummy_dir.mkdir()
    schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(
        sr_mod, "load_dsl_skill",
        lambda path, *, skill_root: _make_stub_skill(input_schema=schema),
    )

    block = asyncio.Event()
    runner, _events, _outbox, _completed = _make_runner(block_on=block)

    async def _run():
        result = await runner.spawn({"skill": "ok", "input": {"text": "hello"}})
        assert result is None, f"expected None on success, got {result!r}"
        assert len(runner.running_names()) == 1
        block.set()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tier 2: invariant 3 — schema + invalid input → structured error, no task
# ---------------------------------------------------------------------------


def test_spawn_invalid_input_returns_structured_error(tmp_path, monkeypatch):
    """Tier 2: input that violates entry_input_schema is rejected
    pre-spawn. spawn() returns a structured error dict carrying
    schema_hint, NO asyncio task is created, and the
    skill_spawn_refused event fires with reason=input_schema_violation."""
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "skill_strict"
    dummy_dir.mkdir()
    schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(
        sr_mod, "load_dsl_skill",
        lambda path, *, skill_root: _make_stub_skill(input_schema=schema),
    )

    runner, events, _outbox, _completed = _make_runner()

    async def _run():
        # Wrong shape: missing required 'text'
        result = await runner.spawn({"skill": "strict", "input": {"wrong_key": "x"}})
        # Structured error dict returned
        assert isinstance(result, dict), f"expected dict, got {result!r}"
        assert result["status"] == "error"
        data = result["data"]
        assert data["kind"] == "spawn_refused"
        assert data["reason"] == "input_schema_violation"
        assert data["skill"] == "strict"
        assert "validation_error" in data
        hint = data["schema_hint"]
        assert hint["skill"] == "strict"
        assert hint["input_schema"] == schema
        assert "retry_hint" in hint
        # No task created
        assert runner.running_names() == []
        # Event emitted
        kinds = [e.type for e in events.all()]
        assert "skill_spawn_refused" in kinds, f"missing event: {kinds}"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tier 2: invariant 4 — skill not found → structured error
# ---------------------------------------------------------------------------


def test_spawn_skill_not_found_returns_structured_error(tmp_path, monkeypatch):
    """Tier 2: SkillNotFoundError during pre-spawn skill load is
    converted to a structured error dict, NO task is created, and the
    skill_spawn_refused event fires with reason=skill_not_found."""
    import reyn.chat.services.skill_runner as sr_mod

    def _raise_not_found(name):
        raise sr_mod.SkillNotFoundError(name, [str(tmp_path)])

    monkeypatch.setattr(sr_mod, "resolve_skill_path", _raise_not_found)

    runner, events, _outbox, _completed = _make_runner()

    async def _run():
        result = await runner.spawn({"skill": "missing", "input": {}})
        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert result["data"]["reason"] == "skill_not_found"
        assert result["data"]["skill"] == "missing"
        assert runner.running_names() == []
        kinds = [e.type for e in events.all()]
        assert "skill_spawn_refused" in kinds

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tier 2: invariant 5 — spawn_for_router propagates the structured error
# ---------------------------------------------------------------------------


def test_spawn_for_router_propagates_pre_spawn_error(tmp_path, monkeypatch):
    """Tier 2: when spawn() rejects pre-spawn (returns dict),
    spawn_for_router forwards that dict verbatim so the router LLM
    sees the schema_hint in the same tool_result round-trip as its
    originating invoke_action call (= sync error path, no async
    [task_completed] correlation needed)."""
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "skill_strict"
    dummy_dir.mkdir()
    schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(
        sr_mod, "load_dsl_skill",
        lambda path, *, skill_root: _make_stub_skill(input_schema=schema),
    )

    runner, _events, _outbox, _completed = _make_runner()

    async def _run():
        out = await runner.spawn_for_router(
            {"skill": "strict", "input": {"bad": "args"}},
            chain_id="c1",
        )
        # The structured error dict from spawn() must flow through
        # spawn_for_router unchanged.
        assert out["status"] == "error"
        assert out["data"]["kind"] == "spawn_refused"
        assert out["data"]["reason"] == "input_schema_violation"
        assert out["data"]["schema_hint"]["input_schema"] == schema
        # Crucially, the legacy "could not be spawned" fallback message
        # must NOT be the surface — that wording loses schema_hint.
        assert "could not be spawned" not in str(out)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Issue #644: pre-spawn skill load is reused by _run_one_skill (no 2x I/O)
# ---------------------------------------------------------------------------


def test_spawn_passes_pre_loaded_skill_no_second_load(tmp_path, monkeypatch):
    """Tier 2: spawn() hands its validated Skill to _run_one_skill via
    ``pre_loaded_skill``, so the async task body skips the duplicate
    resolve_skill_path + load_dsl_skill that happened pre-issue-#644.
    """
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "skill_reuse"
    dummy_dir.mkdir()

    resolve_calls = {"n": 0}
    load_calls = {"n": 0}

    def _counting_resolve(name):
        resolve_calls["n"] += 1
        return dummy_dir, tmp_path

    def _counting_load(path, *, skill_root):
        load_calls["n"] += 1
        return _make_stub_skill(input_schema=None)

    monkeypatch.setattr(sr_mod, "resolve_skill_path", _counting_resolve)
    monkeypatch.setattr(sr_mod, "load_dsl_skill", _counting_load)

    runner, _events, _outbox, _completed = _make_runner()

    async def _run():
        result = await runner.spawn({"skill": "reuse", "input": {}})
        assert result is None, f"expected None on success, got {result!r}"
        # Allow the asyncio task to step through _run_one_skill's load gate.
        for _ in range(10):
            await asyncio.sleep(0)
        # Pre-issue-#644 both calls fired twice. Post-fix, _run_one_skill
        # receives the already-loaded Skill so the second load is skipped.
        assert load_calls["n"] == 1, (
            f"expected 1 load_dsl_skill call (pre-spawn only), got {load_calls['n']}"
        )
        assert resolve_calls["n"] == 1, (
            f"expected 1 resolve_skill_path call (pre-spawn only), got {resolve_calls['n']}"
        )

    asyncio.run(_run())


def test_run_one_skill_falls_back_to_load_when_pre_loaded_skill_absent(
    tmp_path, monkeypatch,
):
    """Tier 2: when ``_run_one_skill`` is invoked directly without a
    ``pre_loaded_skill`` (= future callers, defensive default),
    it still performs the resolve + load itself. Preserves backward
    compatibility for any non-spawn() entry path.
    """
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "skill_fallback"
    dummy_dir.mkdir()

    resolve_calls = {"n": 0}
    load_calls = {"n": 0}

    def _counting_resolve(name):
        resolve_calls["n"] += 1
        return dummy_dir, tmp_path

    def _counting_load(path, *, skill_root):
        load_calls["n"] += 1
        return _make_stub_skill(input_schema=None)

    monkeypatch.setattr(sr_mod, "resolve_skill_path", _counting_resolve)
    monkeypatch.setattr(sr_mod, "load_dsl_skill", _counting_load)

    runner, _events, _outbox, _completed = _make_runner()

    async def _run():
        # Call _run_one_skill directly with pre_loaded_skill=None.
        await runner._run_one_skill(
            run_id="run-x", skill_name="fallback", input_artifact={},
            chain_id=None, pre_loaded_skill=None,
        )
        assert load_calls["n"] == 1, (
            f"expected 1 load_dsl_skill call (direct invocation fallback), got {load_calls['n']}"
        )
        assert resolve_calls["n"] == 1

    asyncio.run(_run())
