"""Tier 2: skill_version_hash event field (FP-0006 Component A).

Verifies that every run_skill_started event carries a ``skill_version_hash``
field containing the sha256 hex digest of the invoked skill's skill.md, and
that ``_compute_skill_hash`` degrades gracefully to "unknown" when the file
does not exist.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real EventLog, real Workspace, real OpContext.
- invoke_sub_skill is replaced by a plain async callable (not a mock) so
  the full run_skill.handle() code path executes without an LLM backend.
- No private-state assertions; observation is through EventLog.all().
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.run_skill import _compute_skill_hash
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import RunSkillIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.skill.sub_skill_runner import SubSkillResult

# ---------------------------------------------------------------------------
# Plain fake for invoke_sub_skill — NOT a mock
# ---------------------------------------------------------------------------


class _StubInvokeSubSkill:
    """Plain async callable that returns a minimal SubSkillResult.

    Satisfies the invoke_sub_skill interface without any LLM call.
    """

    async def __call__(self, sub_skill, input_artifact, **kwargs) -> SubSkillResult:  # noqa: ARG002
        return SubSkillResult(
            data={"type": "result", "data": {}},
            token_usage=None,
            status="finished",
            phase_artifacts=[],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdlib_skill_path(name: str) -> Path:
    """Return the skill.md path for a bundled stdlib skill."""
    from reyn.skill.skill_paths import stdlib_root
    return stdlib_root() / "skills" / name / "skill.md"


def _make_ctx(tmp_path: Path, events: EventLog) -> "OpContext":  # type: ignore[name-defined]
    from reyn.core.op_runtime.context import OpContext

    ws = Workspace(events=events)
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
    )
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        skill_name="test_skill",
        run_id="fp0006_test",
        current_phase="main",
        # Override the state_dir so run_skill doesn't need a real .reyn dir.
        sub_state_dir_override=str(tmp_path / "sub_state"),
    )


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Test 1: run_skill_started carries skill_version_hash (64-char hex)
# ---------------------------------------------------------------------------


def test_skill_version_hash_in_run_skill_started_event(tmp_path, monkeypatch):
    """Tier 2: run_skill.handle() emits skill_version_hash on run_skill_started.

    Exercises the full handle() path with invoke_sub_skill replaced by a
    plain async callable (_StubInvokeSubSkill) that returns immediately.
    Verifies the emitted event carries a 64-char lowercase hex string.
    """
    monkeypatch.chdir(tmp_path)
    import reyn.core.op_runtime.run_skill as run_skill_mod

    monkeypatch.setattr(
        "reyn.skill.sub_skill_runner.invoke_sub_skill",
        _StubInvokeSubSkill(),
    )

    events = EventLog()
    ctx = _make_ctx(tmp_path, events)
    op = RunSkillIROp(
        kind="run_skill",
        skill="word_stats_demo",
        input={"type": "user_message", "data": {"text": "test"}},
    )

    asyncio.run(run_skill_mod.handle(op, ctx, caller="control_ir"))

    started = [e for e in events.all() if e.type == "run_skill_started"]
    assert started, "run_skill_started event not emitted"
    ev = started[0]
    assert "skill_version_hash" in ev.data, (
        f"run_skill_started missing skill_version_hash field. data={ev.data!r}"
    )
    h = ev.data["skill_version_hash"]
    assert _HEX64_RE.match(h), (
        f"skill_version_hash must be a 64-char hex string; got {h!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: hash is stable across two runs of the same skill
# ---------------------------------------------------------------------------


def test_skill_version_hash_stable_across_runs(tmp_path, monkeypatch):
    """Tier 2: two invocations of the same skill emit the same skill_version_hash.

    Confirms the hash is deterministic (same skill.md bytes → same digest).
    """
    monkeypatch.chdir(tmp_path)
    import reyn.core.op_runtime.run_skill as run_skill_mod

    monkeypatch.setattr(
        "reyn.skill.sub_skill_runner.invoke_sub_skill",
        _StubInvokeSubSkill(),
    )

    def _run_once() -> str:
        events = EventLog()
        ctx = _make_ctx(tmp_path, events)
        op = RunSkillIROp(
            kind="run_skill",
            skill="word_stats_demo",
            input={"type": "user_message", "data": {"text": "test"}},
        )
        asyncio.run(run_skill_mod.handle(op, ctx, caller="control_ir"))
        started = [e for e in events.all() if e.type == "run_skill_started"]
        assert started
        return started[0].data["skill_version_hash"]

    hash1 = _run_once()
    hash2 = _run_once()
    assert hash1 == hash2, (
        f"Hashes differ across runs: {hash1!r} != {hash2!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: _compute_skill_hash returns "unknown" for a missing file
# ---------------------------------------------------------------------------


def test_skill_version_hash_unknown_for_missing_file():
    """Tier 2: _compute_skill_hash returns "unknown" when the file does not exist.

    The runtime must never crash when skill.md is absent (e.g. dynamically-
    constructed skills have no on-disk file).
    """
    result = _compute_skill_hash(Path("/nonexistent/path/to/skill.md"))
    assert result == "unknown", (
        f"Expected 'unknown' for missing file; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: hash changes when skill.md content changes
# ---------------------------------------------------------------------------


def test_skill_version_hash_changes_on_skill_md_edit(tmp_path):
    """Tier 2: _compute_skill_hash produces a different digest after editing skill.md.

    Confirms the hash is content-sensitive — a change to skill.md bytes
    always produces a different hash (SHA-256 collision probability negligible).
    """
    skill_md = tmp_path / "skill.md"
    skill_md.write_bytes(b"version: 1\ncontent: original\n")
    hash_v1 = _compute_skill_hash(skill_md)
    assert _HEX64_RE.match(hash_v1), f"Expected 64-char hex; got {hash_v1!r}"

    skill_md.write_bytes(b"version: 2\ncontent: modified\n")
    hash_v2 = _compute_skill_hash(skill_md)

    assert hash_v1 != hash_v2, (
        "Hash must change when skill.md content changes; got same hash for different content"
    )


# ---------------------------------------------------------------------------
# Test 5: hash matches direct sha256 computation on the file
# ---------------------------------------------------------------------------


def test_skill_version_hash_matches_direct_sha256(tmp_path):
    """Tier 2: _compute_skill_hash result equals hashlib.sha256 computed directly.

    Pins the exact digest algorithm: sha256, full hex, raw bytes.
    """
    skill_md = tmp_path / "skill.md"
    content = b"name: test_skill\nentry: main\n"
    skill_md.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    result = _compute_skill_hash(skill_md)
    assert result == expected, f"Hash mismatch: {result!r} != {expected!r}"
