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


def test_mcp_install_tool_description_is_positive_frame() -> None:
    """Tier 2: mcp_install tool description has no failure-mode caveat.

    The ``_MCP_INSTALL_DESCRIPTION`` in ``src/reyn/tools/mcp_install.py`` is
    read at runtime by the LLM (not by skill authors), so it must not describe
    failure modes the LLM cannot trigger from the current state.
    Per [[feedback_schema_exposure_surface_audit]], all surfaces with
    the same shape get fixed together.
    """
    from reyn.tools.mcp_install import _MCP_INSTALL_DESCRIPTION

    _assert_no_misleading_caveat(_MCP_INSTALL_DESCRIPTION, "mcp_install")
