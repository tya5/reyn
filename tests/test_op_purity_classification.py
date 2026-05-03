"""Tier 2: OS invariant — OP_PURITY classification per op kind.

This test pins the determinism classification used by dispatch_tool to
gate step-event emission AND (for ``world`` purity) skip memo lookup
on resume.

Why pin: resume correctness depends on ``world`` ops being re-executed
on resume rather than replayed from memo, because their result depends
on external state (filesystem read, network read) that may have changed
or been transient (flaky API). Mis-classifying a world op as
``side_effect`` or ``external`` would lock in a transient bad result
forever via memo.

Reference: PR-memo-purity-fix M1 in the active plan.
"""
from __future__ import annotations

import pytest

from reyn.op_runtime.registry import (
    ALL_OP_KINDS,
    OP_PURITY,
    OpPurity,
    get_op_purity,
)


# ---------------------------------------------------------------------------
# Per-op-kind classification
# ---------------------------------------------------------------------------


def test_lint_is_pure():
    """Tier 2: ``lint`` is deterministic (pure computation, no I/O)."""
    assert get_op_purity("lint") is OpPurity.pure


def test_web_fetch_is_world():
    """Tier 2: ``web_fetch`` reads external network state — world purity.

    Resume must re-execute web_fetch rather than replay a recorded
    response, because the upstream resource may have changed.
    """
    assert get_op_purity("web_fetch") is OpPurity.world


def test_web_search_is_world():
    """Tier 2: ``web_search`` is a read-only external query — world purity.

    Search index can return different results across runs (especially
    for transient queries). Memo replay would lock in stale results.
    """
    assert get_op_purity("web_search") is OpPurity.world


def test_file_is_side_effect():
    """Tier 2: ``file`` includes both read and write sub-ops.

    Conservative classification (``side_effect``) because the registry
    cannot distinguish file/read from file/write at this level. Finer
    grained classification belongs in the handler.
    """
    assert get_op_purity("file") is OpPurity.side_effect


def test_mcp_is_external():
    """Tier 2: ``mcp/call_tool`` invokes external systems with side effects.

    Same conservatism as ``file``: read-only mcp APIs (search, get) are
    technically world-pure, but the registry cannot distinguish read
    from write at this level.
    """
    assert get_op_purity("mcp") is OpPurity.external


def test_shell_is_external():
    """Tier 2: ``shell`` runs subprocess with arbitrary side effects."""
    assert get_op_purity("shell") is OpPurity.external


def test_run_skill_is_external():
    """Tier 2: ``run_skill`` spawns a child skill with its own side effects."""
    assert get_op_purity("run_skill") is OpPurity.external


def test_ask_user_is_side_effect():
    """Tier 2: ``ask_user`` mutates user state (intervention queue)."""
    assert get_op_purity("ask_user") is OpPurity.side_effect


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_unknown_kind_defaults_to_side_effect():
    """Tier 2: unknown op kind defaults to ``side_effect`` (conservative).

    A new op kind without an explicit purity entry is treated as
    side-effecting so step events are emitted (resume can handle
    ambiguity). Better to over-emit events than to silently skip
    correctness-critical ones.
    """
    assert get_op_purity("not_a_real_kind") is OpPurity.side_effect


# ---------------------------------------------------------------------------
# Coverage check — every registered kind has an explicit purity
# ---------------------------------------------------------------------------


def test_all_known_op_kinds_have_explicit_purity():
    """Tier 2: every entry in ALL_OP_KINDS has an explicit OP_PURITY entry.

    Defends against drift: adding a new op kind without classifying
    purity would silently fall through to the side_effect default,
    which is safe-ish but masks design intent. Force the decision at
    add-time.
    """
    missing = [k for k in ALL_OP_KINDS if k not in OP_PURITY]
    assert missing == [], (
        f"op kinds in ALL_OP_KINDS must have explicit OP_PURITY: {missing}"
    )
