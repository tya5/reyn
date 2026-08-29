"""Tier 2: #5428 — a real PUBLIC read of "what env would this agent's
hook get right now" (``Session.hook_env_snapshot()``), with a real
production consumer (``reyn doctor``) landed in the SAME PR — architect's
own reversal (issuecomment on #5428): a public method with only a test
consumer is the exact #4866 shape #5442 already spent a PR closing 3
instances of.

Before this: the only reader was #5426's own test, reaching through TWO
private hops (``session._hook_dispatcher._hook_process_context()``); an
operator had no way to look at all.

Witnesses (architect's own #5428 table):
    1. All 4 REYN_* keys readable from the public surface.
    2. Moving base_dir (session-layer config.yaml override) changes the
       value on the NEXT read — live, not frozen at construction.
    3. Strip: freezing the read at construction time reds witness 2.

Real Session/HookDispatcher throughout — no mocks.
"""
from __future__ import annotations

from pathlib import Path

from tests._support.agent_session import make_session

# ── witness 1: all 4 keys readable from the public surface ────────────────


def test_hook_env_snapshot_exposes_all_four_reyn_keys(tmp_path: Path) -> None:
    """Tier 2: witness ① — Session.hook_env_snapshot() (the public
    surface #5428 required) returns exactly the 4 REYN_* keys
    HookProcessContext.as_env() defines, sourced from a REAL session."""
    session = make_session(
        agent_name="alpha",
        workspace_state_dir=tmp_path / ".reyn",
        snapshot_path=tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json",
    )
    env = session.hook_env_snapshot()
    assert set(env.keys()) == {
        "REYN_PROJECT_DIR", "REYN_AGENT_BASE_DIR", "REYN_AGENT_NAME",
        "REYN_AGENT_STATE_DIR",
    }
    assert env["REYN_AGENT_NAME"] == "alpha"


# ── witness 2/3: live, not frozen — with its own strip ─────────────────────


def test_moving_base_dir_changes_the_value_on_the_next_read(tmp_path: Path) -> None:
    """Tier 2: witness ② — writing a NEW base_dir override into this
    session's own <session_state_dir>/config.yaml AFTER construction
    changes what the NEXT hook_env_snapshot() call reports — a live
    read, never a value frozen at Session construction time.

    Strip-falsifier (recorded here, executed by hand this session):
    caching ``self._build_hook_process_context()``'s own return value
    at construction (e.g. `self._hook_ctx = self._build_hook_process_
    context()` in __init__, `hook_env_snapshot` returning `self._hook_
    ctx.as_env()`) turns this red — the second read would keep
    reporting the ORIGINAL base_dir, never observing the override
    written after construction. Verified locally."""
    snapshot_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
    session = make_session(
        agent_name="alpha",
        workspace_state_dir=tmp_path / ".reyn",
        snapshot_path=snapshot_path,
    )
    before = session.hook_env_snapshot()["REYN_AGENT_BASE_DIR"]

    override_dir = tmp_path / "narrowed"
    override_dir.mkdir()
    session_config = snapshot_path.parent / "config.yaml"
    session_config.write_text(f"base_dir: {override_dir}\n", encoding="utf-8")

    after = session.hook_env_snapshot()["REYN_AGENT_BASE_DIR"]

    assert after != before, (
        "a base_dir override written after construction must change the "
        "NEXT hook_env_snapshot() read — a frozen value would keep "
        f"reporting {before!r}"
    )
    assert after == str(override_dir.resolve())
