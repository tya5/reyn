"""Tier 2: #4200 2/2 — the spawn-time ``base_dir`` write: restrict-only
validation (LLM-authored) + persistence into the child's own
``config.yaml`` (the #4200 1/2 session-layer read side).

Mirrors #3556's own narrowing-inheritance test shape (same restrict-only
concern, same LLM-writable-argument threat model): ``RouterHostAdapter.
spawn_session`` validates the requested ``base_dir`` against the SPAWNER's
own EFFECTIVE ``base_dir`` (#4200 1/2's resolved value) before persisting
it into the child's ``config.yaml`` — reject, never clamp, per #4179.

⚠️ Restrict-only here is NOT a system-wide invariant (lead-coder review):
it gates only the LLM-authored ``spawn_session`` argument. An OPERATOR
directly hand-editing a session's own ``config.yaml`` never reaches this
check at all — that is correct (the operator owns the envelope), and this
module's last test demonstrates it explicitly so a future reader does not
mistake "restrict-only for the LLM path" for "base_dir never widens,
anywhere".

Real objects throughout — real ``AgentRegistry``/``Session``/
``RouterHostAdapter`` construction, real filesystem ``tmp_path`` roots. No
mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


async def _registry_with_live_parent(
    tmp_path: Path, *, parent_base_dir: Path,
) -> "tuple[AgentRegistry, object]":
    """A real ``AgentRegistry`` + a LIVE spawner session (itself spawned
    through the production ``spawn_session_recorded`` seam, #3556's own
    style — the registry's own agent-level "main" session is a
    non-loading accessor and is never auto-constructed by ``create()``
    alone, so a genuinely-live spawner needs to come from a real spawn)."""
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            workspace_base_dir=parent_base_dir,
            workspace_state_dir=tmp_path / ".reyn",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent",
        presentation_consumer=None, intervention_bridge=None,
    )
    parent = reg.get_session("worker", sid)
    assert parent is not None
    return reg, parent


async def _spawn(parent: object, *, base_dir: "str | None") -> dict:
    """Drive the REAL, production ``RouterHostAdapter.spawn_session`` for
    *parent* — the exact site restrict-only validation lives at."""
    # Bypass the full tool-dispatch scaffolding (#3556's own style already
    # covers that wiring) — call the production method directly with the
    # SAME kwargs the tool handler forwards.
    return await parent._router_host.spawn_session(
        request="p4200-child", mode="persistent", narrowing=None,
        base_dir=base_dir, chain_id="p4200-chain",
    )


# ── Gate 1: within the parent's subtree — accepted + persisted ──────────────


@pytest.mark.asyncio
async def test_a_base_dir_inside_the_parents_subtree_is_accepted_and_persisted(
    tmp_path: Path,
) -> None:
    """Tier 2: a restrict-only-VALID request (a real subdirectory of the
    spawner's own base_dir) is accepted, and the child's own
    <session_state_dir>/config.yaml carries it — the #4200 1/2 session
    layer a later read resolves."""
    parent_base_dir = tmp_path / "parent"
    child_subdir = parent_base_dir / "child-workspace"
    child_subdir.mkdir(parents=True)
    reg, parent = await _registry_with_live_parent(tmp_path, parent_base_dir=parent_base_dir)

    result = await _spawn(parent, base_dir=str(child_subdir))

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    assert cfg.is_file()
    import yaml
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["base_dir"] == str(child_subdir)


@pytest.mark.asyncio
async def test_a_relative_base_dir_resolves_against_the_parents_own_base_dir(
    tmp_path: Path,
) -> None:
    """Tier 2: a relative path resolves against the SPAWNER's own base_dir
    (matching #4200's own required semantics — relative resolution is
    parent-anchored, mirroring an ordinary relative-path read)."""
    parent_base_dir = tmp_path / "parent"
    (parent_base_dir / "sub").mkdir(parents=True)
    reg, parent = await _registry_with_live_parent(tmp_path, parent_base_dir=parent_base_dir)

    result = await _spawn(parent, base_dir="sub")

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    import yaml
    assert (
        yaml.safe_load(cfg.read_text(encoding="utf-8"))["base_dir"]
        == str((parent_base_dir / "sub").resolve())
    )


# ── Gate 2: outside the parent's subtree — rejected, not clamped ────────────


@pytest.mark.asyncio
async def test_a_base_dir_outside_the_parents_subtree_is_rejected_not_clamped(
    tmp_path: Path,
) -> None:
    """Tier 2: the #4179 lesson — a restrict-only-INVALID request (outside
    the spawner's own base_dir) is REJECTED (status=error), never silently
    clamped into the floor, and the rejection message names the ACTUAL
    boundary (the spawner's own base_dir) so the model knows what to ask
    for next. Nothing is persisted for the (never-created) child."""
    parent_base_dir = tmp_path / "parent"
    parent_base_dir.mkdir(parents=True)
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir(parents=True)
    _reg, parent = await _registry_with_live_parent(tmp_path, parent_base_dir=parent_base_dir)

    result = await _spawn(parent, base_dir=str(outside_dir))

    assert result["status"] == "error"
    assert result["kind"] == "base_dir_outside_parent"
    assert str(parent_base_dir.resolve()) in result["error"], (
        f"rejection message does not name the actual parent boundary: {result['error']!r}"
    )
    assert str(outside_dir.resolve()) in result["error"], (
        f"rejection message does not name the requested (rejected) path: {result['error']!r}"
    )


@pytest.mark.asyncio
async def test_a_sibling_directory_one_level_up_is_rejected(tmp_path: Path) -> None:
    """Tier 2: the off-by-one-directory shape — a SIBLING of the parent's
    base_dir (same parent-of-parent, NOT a subdirectory) must be rejected;
    a naive string-prefix check (``str(candidate).startswith(str(parent))``)
    would wrongly ACCEPT ``.../parent-other`` against a floor of
    ``.../parent`` (prefix match without a path-boundary check)."""
    parent_base_dir = tmp_path / "parent"
    parent_base_dir.mkdir(parents=True)
    sibling_dir = tmp_path / "parent-other"  # shares the "parent" STRING prefix
    sibling_dir.mkdir(parents=True)
    _reg, parent = await _registry_with_live_parent(tmp_path, parent_base_dir=parent_base_dir)

    result = await _spawn(parent, base_dir=str(sibling_dir))

    assert result["status"] == "error"
    assert result["kind"] == "base_dir_outside_parent"


# ── Gate 3: omitted — inherits the parent's base_dir unchanged ──────────────


@pytest.mark.asyncio
async def test_omitted_base_dir_inherits_the_parents_base_dir_unchanged(
    tmp_path: Path,
) -> None:
    """Tier 2: regression guard — #4200's own required default (no
    specification → the child inherits the spawner's base_dir). Nothing is
    written to the child's config.yaml at all (byte-identical to
    pre-#4200 spawn behavior for a caller that opts into nothing)."""
    parent_base_dir = tmp_path / "parent"
    parent_base_dir.mkdir(parents=True)
    reg, parent = await _registry_with_live_parent(tmp_path, parent_base_dir=parent_base_dir)

    result = await _spawn(parent, base_dir=None)

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    assert not cfg.is_file(), (
        "a base_dir-less spawn wrote a config.yaml — the pre-#4200 default "
        "(nothing written, agent-level inheritance) regressed"
    )


# ── Gate 4: restrict-only is NOT a system-wide invariant ────────────────────


@pytest.mark.asyncio
async def test_an_operator_hand_edit_of_the_sessions_own_config_bypasses_restrict_only(
    tmp_path: Path,
) -> None:
    """Tier 2: the boundary lead-coder's review named explicitly — restrict-
    only gates the LLM-AUTHORED spawn_session argument only.
    ``Session._read_base_dir_override`` (#4200 1/2's read side) has no
    notion of "who wrote this file" or "what is the parent's floor"; an
    operator hand-editing a session's OWN <session_state_dir>/config.yaml
    directly (never routing through ``RouterHostAdapter.spawn_session``)
    is honored VERBATIM, with no containment check against anything — by
    design (the operator owns the envelope), not a gap this module's other
    gates contradict. Drives the REAL read path (the actual chat-router
    op-context, same as #4200 1/2's own tests), not a raw yaml re-parse —
    proving the production consumer sees the unfiltered value, not just
    that the file contains it."""
    parent_base_dir = tmp_path / "parent"
    parent_base_dir.mkdir(parents=True)
    outside_dir = tmp_path / "nowhere-near-any-parent-floor"
    outside_dir.mkdir(parents=True)
    _reg, session = await _registry_with_live_parent(tmp_path, parent_base_dir=parent_base_dir)

    cfg_dir = Path(session._snapshot_path).parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        f"name: s\nbase_dir: {outside_dir}\n", encoding="utf-8",
    )

    resolved = session._router_op_context_source.build().workspace.base_dir
    assert resolved == outside_dir, (
        f"an operator's direct config.yaml edit resolved to {resolved!r}, not the "
        f"written {outside_dir!r} — restrict-only should not be reachable from "
        f"this surface at all, but this asserts it explicitly rather than assuming it"
    )
