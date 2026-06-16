"""Tier 2: FP-0016 Component D — credential scoping at the run_skill boundary.

Invariants covered:

  1. Sub-skill with required_credentials: ["foo"] → the sub_skill_credential_scope
     event records allowed_keys=["foo"].
  2. Sub-skill with required_credentials: ["*"] (default) + parent scope ["foo"]
     → effective scope ["foo"] (parent-cap intersection).
  3. Sub-skill with required_credentials: ["foo", "bar"] + parent scope ["foo", "baz"]
     → effective scope ["foo"] (explicit intersection).
  4. sub_skill_credential_scope event is emitted with the correct allowed_keys payload
     before invoke_sub_skill runs (P6 audit guarantee).
  5. CredentialScopeError is raised when code tries to read a non-allowed key
     via ScopedSecretStore.get(key).

Design notes (testing policy alignment):

- No MagicMock / AsyncMock / patch.
- Tests 1-4 call op_runtime.execute_op() with a real RunSkillIROp and a real
  OpContext. The invoke_sub_skill call that follows the scoping logic will fail
  (no live LLM), but the sub_skill_credential_scope event is emitted BEFORE
  invoke_sub_skill runs, so event assertions are valid regardless of whether
  the sub-skill completes. execute_op captures the downstream error in the
  returned dict (status="error"), which is expected and not asserted upon.
- Test 5 exercises ScopedSecretStore.get() directly — credential scope checking
  is OS-deterministic and does not depend on LLM output.
- All skill.md fixtures are created in tmp_path at test time (no permanent files).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.schemas.models import RunSkillIROp
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.secrets.store import CredentialScopeError, ScopedSecretStore
from reyn.workspace.workspace import Workspace

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_minimal_skill(
    skill_dir: Path,
    *,
    name: str = "test_skill",
    required_credentials: list[str] | None = None,
) -> Path:
    """Write a minimal skill.md + phases/<entry>.md + artifacts/<output>.yaml
    into skill_dir. Returns the path to skill.md.

    Uses ``user_message`` (stdlib artifact) as both entry input and final output
    to avoid needing to duplicate the artifact schema. The final_output field
    is set to ``user_message``, which is resolved from the stdlib at load time.

    ``required_credentials`` is written into frontmatter when provided. When
    None, the field is omitted (loader defaults to ["*"]).
    """
    skill_dir.mkdir(parents=True, exist_ok=True)
    phases_dir = skill_dir / "phases"
    phases_dir.mkdir(exist_ok=True)

    # Skill frontmatter — use "user_message" (stdlib artifact) for both
    # entry phase input and final_output so no local artifact yaml is needed.
    creds_line = ""
    if required_credentials is not None:
        items = ", ".join(f'"{k}"' for k in required_credentials)
        creds_line = f"\nrequired_credentials: [{items}]"

    skill_md = skill_dir / "skill.md"
    skill_md.write_text(
        f"---\n"
        f"type: skill\n"
        f"name: {name}\n"
        f"entry: entry_phase\n"
        f"final_output: user_message\n"
        f"finish_criteria:\n"
        f"  - Done.\n"
        f"{creds_line}\n"
        f"---\n\n"
        f"entry_phase -> null\n",
        encoding="utf-8",
    )

    # Entry phase — can_finish=true so the LLM can finish immediately
    (phases_dir / "entry_phase.md").write_text(
        "---\n"
        "type: phase\n"
        "name: entry_phase\n"
        "input: user_message\n"
        "input_description: user input\n"
        "role: assistant\n"
        "can_finish: true\n"
        "---\n\n"
        "Process the user request and return a result.\n",
        encoding="utf-8",
    )

    return skill_md


def _make_ctx(tmp_path: Path, *, secret_store: ScopedSecretStore | None = None) -> OpContext:
    """Construct a minimal real OpContext for test use."""
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        secret_store=secret_store,
    )


def _find_event(events: list[Any], event_type: str) -> dict | None:
    """Return the first event of event_type, or None."""
    for ev in events:
        if ev.type == event_type:
            return ev.data
    return None


def _make_run_skill_op(skill_path: str) -> RunSkillIROp:
    return RunSkillIROp(
        kind="run_skill",
        skill=skill_path,
        input={"type": "user_message", "data": {"text": "hello"}},
    )


# ---------------------------------------------------------------------------
# Test 1: Sub-skill required_credentials=["foo"] → scope is {"foo"}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_required_credentials_recorded_in_event(tmp_path: Path) -> None:
    """Tier 2: required_credentials=["foo"] → sub_skill_credential_scope event has allowed_keys=["foo"]."""
    skill_dir = tmp_path / "skills" / "skill_foo"
    skill_md = _write_minimal_skill(skill_dir, name="skill_foo", required_credentials=["foo"])

    ctx = _make_ctx(tmp_path)
    op = _make_run_skill_op(str(skill_md))

    # execute_op will fail at invoke_sub_skill (no LLM), but the scoping event
    # is emitted before that — the result status is not asserted.
    await execute_op(op, ctx, caller="control_ir")

    ev_data = _find_event(ctx.events.all(), "sub_skill_credential_scope")
    assert ev_data is not None, "sub_skill_credential_scope event was not emitted"
    assert ev_data["allowed_keys"] == ["foo"]


# ---------------------------------------------------------------------------
# Test 2: Sub-skill required_credentials=["*"] (default) + parent scope ["foo"]
#         → effective scope is ["foo"] (parent-cap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_credentials_capped_by_parent_scope(tmp_path: Path) -> None:
    """Tier 2: required_credentials=["*"] + parent scope ["foo"] → effective scope ["foo"]."""
    skill_dir = tmp_path / "skills" / "skill_default"
    # Omit required_credentials → defaults to ["*"]
    skill_md = _write_minimal_skill(skill_dir, name="skill_default", required_credentials=None)

    # Parent has a restricted scope of ["foo"]
    parent_store = ScopedSecretStore(allowed_keys=["foo"])
    ctx = _make_ctx(tmp_path, secret_store=parent_store)
    op = _make_run_skill_op(str(skill_md))

    await execute_op(op, ctx, caller="control_ir")

    ev_data = _find_event(ctx.events.all(), "sub_skill_credential_scope")
    assert ev_data is not None, "sub_skill_credential_scope event was not emitted"
    # Sub-skill wanted ["*"] but parent only allows ["foo"], so cap applies.
    assert ev_data["allowed_keys"] == ["foo"]


# ---------------------------------------------------------------------------
# Test 3: Sub-skill required_credentials=["foo", "bar"] + parent scope ["foo", "baz"]
#         → effective scope is ["foo"] (explicit intersection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_intersection_with_parent_scope(tmp_path: Path) -> None:
    """Tier 2: required_credentials=["foo","bar"] + parent scope ["foo","baz"] → effective scope ["foo"]."""
    skill_dir = tmp_path / "skills" / "skill_intersect"
    skill_md = _write_minimal_skill(
        skill_dir, name="skill_intersect", required_credentials=["foo", "bar"]
    )

    parent_store = ScopedSecretStore(allowed_keys=["foo", "baz"])
    ctx = _make_ctx(tmp_path, secret_store=parent_store)
    op = _make_run_skill_op(str(skill_md))

    await execute_op(op, ctx, caller="control_ir")

    ev_data = _find_event(ctx.events.all(), "sub_skill_credential_scope")
    assert ev_data is not None, "sub_skill_credential_scope event was not emitted"
    # ["foo", "bar"] ∩ ["foo", "baz"] = ["foo"]
    assert ev_data["allowed_keys"] == ["foo"]


# ---------------------------------------------------------------------------
# Test 4: sub_skill_credential_scope event shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_scope_event_payload_shape(tmp_path: Path) -> None:
    """Tier 2: sub_skill_credential_scope event carries skill name and allowed_keys list."""
    skill_dir = tmp_path / "skills" / "skill_shape"
    skill_md = _write_minimal_skill(
        skill_dir, name="skill_shape", required_credentials=["token_a", "token_b"]
    )

    ctx = _make_ctx(tmp_path)
    op = _make_run_skill_op(str(skill_md))

    await execute_op(op, ctx, caller="control_ir")

    ev_data = _find_event(ctx.events.all(), "sub_skill_credential_scope")
    assert ev_data is not None, "sub_skill_credential_scope event was not emitted"
    # Payload must contain the skill field (op.skill) and allowed_keys list.
    assert "skill" in ev_data, f"'skill' missing from event payload: {ev_data}"
    assert "allowed_keys" in ev_data, f"'allowed_keys' missing from event payload: {ev_data}"
    # Sorted list — deterministic order.
    assert ev_data["allowed_keys"] == ["token_a", "token_b"]


# ---------------------------------------------------------------------------
# Test 5: CredentialScopeError raised for non-allowed key reads
# ---------------------------------------------------------------------------


def test_credential_scope_error_raised_for_non_allowed_key(tmp_path: Path) -> None:
    """Tier 2: ScopedSecretStore.get(key) raises CredentialScopeError for keys outside allowed_keys.

    This test exercises the OS enforcement surface that would be reached when
    skill code calls ctx.secret_store.get(key). The store is constructed with
    allowed_keys=["allowed_key"] and we assert that reading "forbidden_key"
    raises CredentialScopeError.
    """
    # Write a minimal secrets file so the store has something to check against.
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("allowed_key=secret_value\nforbidden_key=hidden\n", encoding="utf-8")

    store = ScopedSecretStore(allowed_keys=["allowed_key"], path=secrets_file)

    # Allowed key must succeed.
    assert store.get("allowed_key") == "secret_value"

    # Forbidden key must raise CredentialScopeError (a PermissionError subclass).
    with pytest.raises(CredentialScopeError) as exc_info:
        store.get("forbidden_key")

    assert "forbidden_key" in str(exc_info.value), (
        "CredentialScopeError message must name the disallowed key"
    )


# ---------------------------------------------------------------------------
# Test 6: No-parent-scope + required_credentials=["*"] → event records ["*"]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_parent_scope_unrestricted_records_star(tmp_path: Path) -> None:
    """Tier 2: No parent scope + required_credentials=["*"] → allowed_keys=["*"] in event."""
    skill_dir = tmp_path / "skills" / "skill_star"
    # Omit required_credentials → defaults to ["*"]
    skill_md = _write_minimal_skill(skill_dir, name="skill_star", required_credentials=None)

    # No parent scope (ctx.secret_store = None)
    ctx = _make_ctx(tmp_path, secret_store=None)
    op = _make_run_skill_op(str(skill_md))

    await execute_op(op, ctx, caller="control_ir")

    ev_data = _find_event(ctx.events.all(), "sub_skill_credential_scope")
    assert ev_data is not None, "sub_skill_credential_scope event was not emitted"
    assert ev_data["allowed_keys"] == ["*"]
