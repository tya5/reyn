"""Tier 2: #6151 — two ``make_session()`` calls sharing the SAME
``agent_name``/``session_id`` defaults (``"alpha"``/``"main"``, respectively
— the second because MANY tests assert on it directly, see
``tests/core/test_registry_list_rewind_points_1f.py`` et al, so it cannot
change) used to compute the SAME real filesystem
``<tempdir>/reyn/alpha/main`` for ``Session._child_temp_dir``. Under
``pytest -n auto``, unrelated tests in DIFFERENT worker processes hit this
exact path concurrently — one worker's create/cleanup racing another's
mid-write use produced the CI-measured failures (``session temp_dir is not
writable``, ``the command must queue at least one new block``), lead-coder
traced to `tests/interfaces/test_5837_exec_slash_stage1.py:29`'s fixed
``agent_name="alpha"``.

Reads the resolved temp dir through the PUBLIC surface
``session.router_host.make_router_op_context().temp_dir`` — the same
sanctioned path ``test_5184_session_temp_lifetime.py`` already
establishes — never ``session._child_temp_dir`` directly (private state,
testing.md/Tier 4).

Real ``Session``/``make_session`` throughout — no mocks.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from reyn.core.events.state_log import StateLog
from tests._support.agent_session import make_session


def _resolved_temp_dir(session) -> str:
    """The one PUBLIC read of the resolved child-temp path -- also creates
    it on disk (the real, lazy production path), so callers clean up.
    ``default_sandbox_policy["temp_dir"]`` (``resolve_sandbox_policy``'s
    own public dict-shaped return) is where the SAME value lead-coder's
    own CI failure message names verbatim (``session temp_dir is not
    writable: '/tmp/reyn/alpha/main'``) ends up."""
    ctx = session.router_host.make_router_op_context()
    return ctx.default_sandbox_policy["temp_dir"]


def _session(tmp_path: Path, name: str, *, isolate: bool = True):
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / f"{name}.wal"),
        snapshot_path=tmp_path / f"{name}-snap.json",
        isolate_child_temp_dir=isolate,
    )


def test_two_sessions_sharing_agent_and_session_id_defaults_get_isolated_temp_dirs(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- #6151's own fix. Two sessions built with nothing
    but the SAME defaults (``agent_name="alpha"``, ``session_id`` omitted
    -> ``"main"``) must never resolve to the same real child-temp path --
    the exact shape that collided under `-n auto` workers.

    FALSIFY: passing ``isolate=False`` to both (the pre-#6151 shape) makes
    this go red -- both land on the identical
    ``<tempdir>/reyn/alpha/main`` path."""
    session_a = _session(tmp_path / "a", "a")
    session_b = _session(tmp_path / "b", "b")
    dir_a = _resolved_temp_dir(session_a)
    dir_b = _resolved_temp_dir(session_b)
    try:
        assert dir_a != dir_b, (
            f"two independent sessions collided on the same real filesystem "
            f"path: {dir_a!r} -- the #6151 defect shape that raced under "
            f"parallel xdist workers"
        )
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


def test_isolate_child_temp_dir_false_preserves_the_pre_6151_shared_formula(
    tmp_path: Path,
) -> None:
    """Tier 2: deny -- the explicit opt-out (`test_5184_session_temp_
    lifetime.py`'s own use) must still produce the EXACT pre-#6151
    formula-derived path, byte-identical, so a test verifying that
    formula itself keeps working unchanged."""
    session_a = _session(tmp_path / "a", "a", isolate=False)
    session_b = _session(tmp_path / "b", "b", isolate=False)
    dir_a = _resolved_temp_dir(session_a)
    dir_b = _resolved_temp_dir(session_b)
    try:
        assert dir_a == dir_b, (
            "isolate_child_temp_dir=False must reproduce the shared, formula-"
            "derived path -- this is the control arm proving the opt-out is a "
            "real escape hatch, not a no-op"
        )
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
