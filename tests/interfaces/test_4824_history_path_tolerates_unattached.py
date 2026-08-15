"""Tier 2: #4824 — ``RegistryReadModel.history_path`` tolerates an unattached
registry, closing the one seam ``run_repl``'s own #3671 P2 docstring claimed
(inaccurately, until this fix) was already covered.

Reproduced via a piped/non-TTY ``echo "..." | reyn chat``: the interactive
path schedules ``registry.attach(name)`` as a background task and starts the
REPL immediately, with essentially no ``await`` before
``FileHistory(str(read_model.history_path))`` runs
(``interfaces/repl/client_driver.py``) — so the background attach task had
not ticked even once. Not a slow-WAL-restore race; it fired on ~every such
invocation.

#3671 P2's own docstring: "Every seam below already tolerated an unattached
registry" and names three specific accessors it audited
(``InProcessTransport``'s ``_attached() is None`` guards,
``_wire_focus_listeners(None)``, ``RegistryReadModel.snapshot`` returning
``None``) — ``history_path`` was not among them and hard-raised instead.
Fixed by giving ``RegistryReadModel`` the caller's intended target
``agent_name`` (known before attach can possibly succeed, same reasoning
the docstring already uses for ``name`` itself) so ``history_path`` can fall
back to :meth:`~reyn.runtime.registry.AgentRegistry.agent_workspace_dir` — a
pure path derivation needing no live :class:`Session`, so it costs nothing
and races nothing.

Real ``AgentRegistry``/``Session`` — no mocks, per the testing policy.
Harness mirrors ``test_textual_chat_attach_state_3671_p3.py``'s own
``_registry`` helper (the sibling #3671 P2/P3 test file).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


def test_history_path_falls_back_when_unattached_with_a_target_name(
    tmp_path,
) -> None:
    """Tier 2: the exact defect — a registry with NOTHING attached yet
    (``registry.attached_session()`` is ``None``) must not raise when the
    read-model was given the target agent name at construction."""
    reg = _registry(tmp_path)
    read_model = RegistryReadModel(reg, agent_name="alpha")

    path = read_model.history_path

    assert path == reg.agent_workspace_dir("alpha") / ".input_history"


def test_history_path_without_a_target_name_still_raises(tmp_path) -> None:
    """Tier 2: accept-side sibling — a caller that supplies NO agent_name
    (the pre-#4824 construction shape, still valid for any OTHER caller of
    this class) must keep the original, honest failure — there is nothing
    to fall back to."""
    reg = _registry(tmp_path)
    read_model = RegistryReadModel(reg)

    with pytest.raises(RuntimeError, match="no attached session"):
        _ = read_model.history_path


@pytest.mark.asyncio
async def test_the_fallback_path_matches_the_attached_sessions_own_path(
    tmp_path,
) -> None:
    """Tier 2: the fallback must be the SAME path an attached session's own
    ``workspace_dir`` reports — not a temporary stand-in later silently
    replaced (lead-coder's own condition: "two truths" is not acceptable).
    Reads ``history_path`` BEFORE attach, then again AFTER, on the SAME
    read-model instance."""
    reg = _registry(tmp_path)
    read_model = RegistryReadModel(reg, agent_name="alpha")

    before_attach = read_model.history_path

    await reg.attach("alpha")
    after_attach = read_model.history_path

    assert before_attach == after_attach, (
        "the unattached fallback and the post-attach real session path "
        f"must agree: {before_attach!r} != {after_attach!r}"
    )


@pytest.mark.asyncio
async def test_reproduces_the_original_crash_shape_and_no_longer_raises(
    tmp_path,
) -> None:
    """Tier 2b: reproduces the ACTUAL race from ``chat.py``'s interactive
    path — ``attach()`` scheduled as a background task via
    ``asyncio.create_task``, then ``history_path`` read with NO ``await``
    in between (the exact shape ``run_repl`` → ``run_chat_client`` hits:
    essentially zero yield points before ``FileHistory(...)`` construction).
    Before #4824 this raised on ~every piped/non-TTY invocation; must not
    raise now."""
    import asyncio

    reg = _registry(tmp_path)
    read_model = RegistryReadModel(reg, agent_name="alpha")

    attach_task = asyncio.create_task(reg.attach("alpha"))
    try:
        # No await here — this is the exact gap that made the crash fire.
        path = read_model.history_path
        assert path == reg.agent_workspace_dir("alpha") / ".input_history"
    finally:
        await attach_task
