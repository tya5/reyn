"""Tier 2: positive-frame invariant for Tier-3 op descriptions in op_catalog.

When the OS exposes a Tier-3 op (= ``shell`` / ``mcp`` / ``mcp_install``,
etc.) in the LLM-visible op_catalog, its presence in the catalog already
means the precondition (= permission declared + caller-side enabled)
has been satisfied. The op's ``description`` field is read at runtime
by the LLM, NOT by skill authors — so it must NOT describe failure
modes the LLM cannot actually trigger from the current state.

Empirical precedent (FP-0008 sandbox_2 2026-05-28 calibration retry):
the previous shell op description carried a "Tier 3 op (declared +
approved): ... OS rejects at runtime with PermissionError if
undeclared" caveat. Even though the caveat described a state the LLM
could never reach (= the catalog visibility itself was the affirmation
that the permission was declared), a small / cheap LLM read the
caveat as a runtime warning, generated an ``abort`` artifact with
confidence 1.0, and returned a synthetic "permissions.shell: true is
required" reason — without ever attempting to issue the shell op.
All 10 SWE-bench calibration instances aborted at the setup phase.

The fix: replace the runtime-irrelevant caveat with a positive-frame
``Status: enabled — ... do not abort on permission concerns`` clause.
This test pins the invariant so future op-description authors don't
re-introduce the same misleading caveat shape.

Specifically:
  1. ``shell`` op description (= when ``shell_allowed=True``) is
     positive-frame: no "OS rejects" / "PermissionError if undeclared"
     phrasing.
  2. ``mcp`` op description (= when ``mcp_servers`` non-empty) is
     positive-frame: same anti-pattern check.
  3. ``mcp_install`` tool description (= different module path, same
     class of misleading caveat) is positive-frame.

The rule is **negative** (= no banned phrases), not positive (= no
required exact wording), so authors can rewrite the affirmation
freely as long as they don't re-add the failure-mode caveat.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_NEGATIVE_PHRASES = (
    "OS rejects at runtime",
    "PermissionError if undeclared",
    "must also be set to true",
    "(declared + approved)",
)


def _assert_no_misleading_caveat(description: str, op_name: str) -> None:
    """Helper: every negative phrase must be absent from the description."""
    for phrase in _NEGATIVE_PHRASES:
        assert phrase not in description, (
            f"{op_name} description re-introduces the misleading caveat "
            f"phrase {phrase!r} — see "
            f"tests/test_tier3_op_description_positive_frame.py for the "
            f"rationale (FP-0008 sandbox_2 2026-05-28 retry precedent)."
        )


def _build_executor(
    tmp_path: Path,
    *,
    mcp_servers: dict | None = None,
):
    """Construct a minimal ControlIRExecutor under tmp_path (no global state)."""
    import os

    from reyn.core.events.events import EventLog
    from reyn.core.kernel.control_ir_executor import ControlIRExecutor
    from reyn.data.workspace.workspace import Workspace

    os.chdir(tmp_path)  # Workspace anchors to CWD; isolate to tmp
    events = EventLog()
    workspace = Workspace(events=events)
    return ControlIRExecutor(
        workspace=workspace,
        events=events,
        mcp_servers=mcp_servers,
    )


def test_mcp_op_description_is_positive_frame(tmp_path: Path) -> None:
    """Tier 2: call_mcp_tool op description (when servers configured) has no failure-mode caveat.

    #1240 Wave 2b: available_ops() advertises "call_mcp_tool" (chat name alias)
    instead of "mcp" (execution op kind).  Pin updated accordingly.
    """
    # mcp_servers shape: ControlIRExecutor.__init__ reads .get("servers", {}),
    # so the canonical input is {"servers": {<name>: <config>}}.
    executor = _build_executor(
        tmp_path,
        mcp_servers={"servers": {"github": {"transport": "stdio"}}},
    )
    mcp_specs = [s for s in executor.available_ops() if s.kind == "call_mcp_tool"]
    # unpack-enforcement (= behavior pin, not size pin)
    (mcp_spec,) = mcp_specs
    _assert_no_misleading_caveat(mcp_spec.description, "call_mcp_tool")


def test_mcp_install_tool_description_is_positive_frame() -> None:
    """Tier 2: mcp_install tool description has no failure-mode caveat.

    Lives in ``src/reyn/tools/mcp_install.py`` (= different module than
    the control_ir_executor specs), but exhibits the same anti-pattern.
    Per [[feedback_schema_exposure_surface_audit]], all surfaces with
    the same shape get fixed together.
    """
    from reyn.tools.mcp_install import _MCP_INSTALL_DESCRIPTION

    _assert_no_misleading_caveat(_MCP_INSTALL_DESCRIPTION, "mcp_install")


def test_no_tier3_op_uses_skill_author_phrasing(tmp_path: Path) -> None:
    """Tier 2: no Tier-3 op description carries skill-author-doc phrasing.

    "phase.allowed_ops" / "skill.permissions" / similar config-name
    references belong in skill-author docs (= docs/reference/dsl/),
    NOT in the runtime op_catalog the LLM reads. This test catches a
    broader class of "wrong-audience phrasing" than the negative-phrase
    list above.
    """
    executor = _build_executor(
        tmp_path,
        mcp_servers={"servers": {"x": {"transport": "stdio"}}},
    )
    skill_author_phrases = ("phase.allowed_ops", "skill.permissions")
    for spec in executor.available_ops():
        for phrase in skill_author_phrases:
            assert phrase not in spec.description, (
                f"Op {spec.kind!r} description carries skill-author "
                f"phrasing {phrase!r} that is wrong-audience for the "
                f"runtime LLM. Reword as a runtime affirmation."
            )
