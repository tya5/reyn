"""Tier 2: #4366 -- the capability census does not depend, silently, on
``reyn.runtime.router_loop`` having been imported first.

OS invariant: the built-in ``ToolUseScheme``s self-register via an
IMPORT-TIME side effect (each module under ``reyn.tools.schemes.*`` calls
``register_scheme`` at module scope; the registry, ``reyn.tools.scheme``,
singular, is empty until the plural ``schemes`` package -- or one of its
submodules -- is actually imported). Before this fix, the ONLY place in
``src/`` that imported it was ``router_loop.py``, which every real chat
turn imports as a matter of course -- but ``capability_visibility.py``'s
``_reachable_tool_names`` (the #3220 tool census) never imported it itself,
only the registry module, and called ``get_scheme(...)`` assuming a
populated registry. A fresh session's FIRST status-bar render, before any
LLM turn has run, hits this: the registry is empty, both ``get_scheme``
calls return ``None``, and ``None.build_presentation`` raises
``AttributeError`` (owner-reported real-environment crash, #4366).

Real-interpreter witness, not an in-process one: pytest's own process may
already have imported ``reyn.tools.schemes`` via an unrelated test file's
collection (module import order is not guaranteed), which would make an
in-process assertion of "the registry starts empty" unreliable. A fresh
``sys.executable`` subprocess is the only way to genuinely witness the
owner's actual precondition.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def test_capability_visibility_state_survives_a_fresh_interpreter_with_no_router_loop_import(
    out_of_process_reyn: str,
) -> None:
    """Tier 2: a fresh interpreter that builds a ``Session`` and reads
    ``capability_visibility_state()`` WITHOUT ``reyn.runtime.router_loop``
    (or ``reyn.tools.schemes``) ever having been imported -- #4366's real
    precondition -- must not raise, and must leave the scheme registry
    populated afterward (proving the fix's own import actually fired, not
    that this path happens to dodge the registry read some other way)."""
    code = textwrap.dedent(f"""
        import sys, tempfile
        from pathlib import Path

        sys.path.insert(0, {out_of_process_reyn!r})
        sys.path.insert(0, str(Path({out_of_process_reyn!r}).parent))

        from reyn.tools.scheme import registered_scheme_names
        from tests._support.agent_session import make_session

        assert "reyn.runtime.router_loop" not in sys.modules
        assert "reyn.tools.schemes" not in sys.modules
        assert registered_scheme_names() == [], (
            "test precondition: the registry must start empty, or this test "
            "cannot witness the #4366 crash condition at all"
        )

        tmp = Path(tempfile.mkdtemp())
        session = make_session(
            agent_name="probe", workspace_base_dir=tmp, workspace_state_dir=tmp,
        )
        state = session.capability_visibility_state()
        assert isinstance(state, dict)
        assert registered_scheme_names() != [], (
            "capability_visibility_state() returned without ever importing "
            "reyn.tools.schemes -- the fix's own declared import did not fire"
        )
        print("SURVIVED")
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "SURVIVED" in result.stdout


def test_scheme_still_none_after_import_raises_not_attributeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #4366's second, independent fix -- if BOTH the configured
    scheme name and the default somehow still resolve to ``None`` even
    after the registry-populating import (an internal invariant violation,
    since ``get_scheme`` is monkeypatched here to simulate exactly that; no
    real code path can construct this today), the census raises a legible
    ``RuntimeError`` naming both names it tried, not a bare
    ``AttributeError`` on ``None.build_presentation`` -- the crash the
    owner actually hit, and the shape ``or get_scheme(DEFAULT)`` alone
    never guarded against (the right-hand side fails identically to the
    left when the registry itself is the problem, not just an unknown
    configured name)."""
    import tempfile
    from pathlib import Path

    from tests._support.agent_session import make_session

    monkeypatch.setattr("reyn.tools.scheme.get_scheme", lambda name: None)

    tmp = Path(tempfile.mkdtemp())
    session = make_session(
        agent_name="probe", workspace_base_dir=tmp, workspace_state_dir=tmp,
    )
    try:
        session.capability_visibility_state()
    except RuntimeError as exc:
        assert "enumerate-all" in str(exc)
    else:
        raise AssertionError(
            "expected RuntimeError when both scheme lookups return None"
        )
