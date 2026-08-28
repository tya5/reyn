"""Tier 2: #5428 — a genuine public surface answers "what env would this
agent's hook get right now".

Before this: the only read of a session's hook-child env was two private
hops deep (``session._hook_dispatcher._hook_process_context()``, #5426's
own test) — CLAUDE.md's testing policy names this exact shape ("a test
must not depend on private state... if neither exists, that absence is
the finding"). An operator debugging "my hook says this env var is empty"
had no way to look at all — the second, non-test reason this issue exists
(architect ruling, #5428's own issue body).

``Session.hook_env_snapshot()`` returns the resolved VALUES (a plain
``dict[str, str]``, the same 4 ``REYN_*`` keys #5208 finished), never the
callable itself — a caller cannot reach ``HookDispatcher`` internals
through it.

Witness table (architect's, verbatim from the issue):
1. all 4 keys readable from the public surface.
2. moving ``base_dir`` on an already-constructed session changes the
   value — live, not frozen at construction.
3. strip: reverting the live-read to a construction-time-frozen value
   makes witness 2 go RED.

Policy (docs/deep-dives/contributing/testing.md): real instances only —
no ``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

from pathlib import Path

from tests._support.agent_session import make_session


def test_all_four_keys_are_readable_from_the_public_surface(tmp_path: Path) -> None:
    """Tier 2: witness 1 — ``hook_env_snapshot()`` exposes exactly the 4
    ``REYN_*`` keys a real hook child process would see, readable without
    reaching into any private attribute."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    session = make_session(
        agent_name="coder1",
        workspace_state_dir=project_root / ".reyn",
    )

    env = session.hook_env_snapshot()

    assert set(env.keys()) == {
        "REYN_PROJECT_DIR", "REYN_AGENT_BASE_DIR", "REYN_AGENT_NAME",
        "REYN_AGENT_STATE_DIR",
    }
    assert env["REYN_AGENT_NAME"] == "coder1"
    assert Path(env["REYN_PROJECT_DIR"]) == project_root.resolve()


def test_moving_base_dir_changes_the_snapshot_live(tmp_path: Path) -> None:
    """Tier 2: witness 2 — reading the snapshot AFTER narrowing
    ``base_dir`` on an already-constructed session reflects the NEW
    value, not the one captured at construction time. A single read
    right after construction would not distinguish "live" from "frozen
    at construction" — this test writes the SAME ``<session_state_dir>/
    config.yaml`` ``base_dir:`` override an operator/spawn seam would
    write at runtime (#4200/#5081 — Session._workspace_base_dir's own
    docstring: "session-layer override ... IN FRONT OF the agent-layer
    default"), between two reads on the SAME session object, to force
    that distinction. ``narrow_base`` stays ⊆ the project workspace
    (#5081's restrict-only bound would otherwise reject it and fall
    through to the next layer)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    narrow_base = project_root / "narrow-base"
    narrow_base.mkdir()

    session = make_session(
        agent_name="coder1",
        workspace_state_dir=project_root / ".reyn",
    )

    before = session.hook_env_snapshot()
    assert Path(before["REYN_AGENT_BASE_DIR"]) == project_root.resolve()

    session_config = Path(session._snapshot_path).parent / "config.yaml"
    session_config.parent.mkdir(parents=True, exist_ok=True)
    session_config.write_text(f"base_dir: {narrow_base}\n", encoding="utf-8")

    after = session.hook_env_snapshot()
    assert Path(after["REYN_AGENT_BASE_DIR"]) == narrow_base.resolve(), (
        "hook_env_snapshot() must read the session's base_dir override LIVE "
        "on every call, not cache a value captured at Session construction "
        "time"
    )
