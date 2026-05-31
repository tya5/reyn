"""Tier 2: phase Control IR coarse op.kind → registry dispatch coverage.

Post-FP-0039 audit pin (2026-05-18). Confirms that every coarse op kind
``ControlIRExecutor._invoker`` actually routes through the unified
ToolRegistry (= ``invoke_tool(get_default_registry(), op.kind, ...)``)
has a ToolDefinition with ``gates.phase="allow"``. If a future PR adds a
coarse op kind to ``OP_KIND_MODEL_MAP`` but forgets the registry entry,
the kind silently falls back to the legacy ``execute_op`` path and the
ADR-0026 M4 unification claim regresses unnoticed.

The kinds pinned here are the ones FP-0039 §S1 audit found wired:
file / mcp / run_skill / shell / lint / ask_user / web_fetch /
web_search / mcp_install / recall / sandboxed_exec — plus ``compact``
(#272/#1128, phase=allow registry entry). The 6 RAG / internal
kinds (embed / index_* / judge_output / skill_resolve) intentionally
stay on the legacy path and are documented in this test's expected-
legacy set so adding them later is an explicit change, not a silent one.
"""
from __future__ import annotations

import pytest

from reyn.op_runtime.registry import OP_KIND_MODEL_MAP
from reyn.tools import get_default_registry

# Coarse op kinds the FP-0039 audit confirmed dispatch through the
# unified registry. Adding a kind here is a deliberate claim that
# control_ir_executor.py routes via invoke_tool(_registry, op.kind, ...)
# for it.
_REGISTRY_WIRED_KINDS: frozenset[str] = frozenset({
    "ask_user",
    "file",
    "lint",
    "mcp",
    "mcp_install",
    "recall",
    "run_skill",
    "sandboxed_exec",
    "shell",
    "web_fetch",
    "web_search",
    # #272/#1128: compact has a phase=allow registry ToolDefinition, so the op
    # dispatches through the unified registry. The phase compaction CAPABILITY
    # (OpContext.compact_now) is wired for chat now; in phases it is unwired
    # until the B1 follow-up (#1176), so a phase-emitted compact fail-louds with
    # compaction_unavailable rather than silently no-op'ing. Dispatch path is
    # registry-wired either way → classified here.
    "compact",
})

# Coarse op kinds that intentionally still dispatch via the legacy
# op_runtime/<kind>.py path (= ControlIRExecutor._invoker hits the
# execute_op fallback because the registry lookup returns None).
# These are RAG plumbing + internal kernel ops that have no
# router-side analog and don't benefit from the registry unification.
_LEGACY_ONLY_KINDS: frozenset[str] = frozenset({
    "embed",
    "index_drop",
    "index_query",
    "index_write",
    "judge_output",
    "skill_resolve",
})


def test_op_kind_partition_is_total() -> None:
    """Tier 2: every kind in OP_KIND_MODEL_MAP is in exactly one set.

    Catches the regression where a new coarse op kind is added to
    OP_KIND_MODEL_MAP without classifying it as wired vs legacy. Forces
    the author of the new kind to decide explicitly.
    """
    all_kinds = frozenset(OP_KIND_MODEL_MAP.keys())
    classified = _REGISTRY_WIRED_KINDS | _LEGACY_ONLY_KINDS
    missing = all_kinds - classified
    extra = classified - all_kinds
    assert not missing, (
        f"OP_KIND_MODEL_MAP has kinds not classified here: {sorted(missing)}. "
        f"Add to _REGISTRY_WIRED_KINDS or _LEGACY_ONLY_KINDS based on "
        f"whether the kind has a phase=allow ToolDefinition."
    )
    assert not extra, (
        f"Classified kinds not in OP_KIND_MODEL_MAP: {sorted(extra)}. "
        f"Stale entry — remove from the partition sets."
    )
    assert _REGISTRY_WIRED_KINDS.isdisjoint(_LEGACY_ONLY_KINDS), (
        f"Overlap between wired and legacy sets: "
        f"{sorted(_REGISTRY_WIRED_KINDS & _LEGACY_ONLY_KINDS)}"
    )


@pytest.mark.parametrize("kind", sorted(_REGISTRY_WIRED_KINDS))
def test_wired_kind_has_phase_allow_registry_entry(kind: str) -> None:
    """Tier 2: each wired coarse kind has a registry entry with phase=allow.

    Without this gate, ``ControlIRExecutor._invoker`` would skip the
    registry path and fall through to the legacy ``execute_op``,
    breaking the ADR-0026 M4 unification claim for the kind.
    """
    reg = get_default_registry()
    td = reg.lookup(kind)
    assert td is not None, (
        f"coarse op.kind={kind!r} has no registry entry — "
        f"control_ir_executor.py will fall through to execute_op "
        f"(legacy path). Register a ToolDefinition in reyn/tools/."
    )
    assert td.gates.phase == "allow", (
        f"coarse op.kind={kind!r} has gates.phase={td.gates.phase!r}; "
        f"must be 'allow' for ControlIRExecutor._invoker to dispatch "
        f"through the unified registry."
    )


@pytest.mark.parametrize("kind", sorted(_LEGACY_ONLY_KINDS))
def test_legacy_kind_stays_unregistered(kind: str) -> None:
    """Tier 2: each legacy-only coarse kind has no registry entry.

    If a registry entry is added without updating _LEGACY_ONLY_KINDS,
    that's a meaningful architectural move (= the kind has joined the
    unified dispatch path). Force the change to be explicit.
    """
    reg = get_default_registry()
    td = reg.lookup(kind)
    assert td is None, (
        f"coarse op.kind={kind!r} now has a registry entry — if intentional, "
        f"move it from _LEGACY_ONLY_KINDS to _REGISTRY_WIRED_KINDS in this "
        f"test (a deliberate change, not a silent one)."
    )
