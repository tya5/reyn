"""Chainlit-free logic for the /rewind fork picker web surface (ADR-0038 2d-2).

The web analog of the TUI fork picker: bare ``/rewind`` in Chainlit renders the
branch tree as a message with a checkout action per checkpoint. This module is
**chainlit-free** (no ``import chainlit``) so the logic is unit-testable without
the chainlit runtime; the thin ``cl.Message`` / ``cl.Action`` glue lives in
``app.py`` and consumes these specs.

Reuses the pure ``build_branch_tree_rows`` (TUI 2b) — one tree-grouping source
across TUI + web (lead-approved cross-surface reuse).
"""
from __future__ import annotations

from reyn.chat.tui.widgets._branch_tree import (
    ROW_CHECKPOINT,
    build_branch_tree_rows,
)


def build_rewind_action_specs(
    branches: list[dict],
    checkpoints: list[dict],
) -> list[dict]:
    """Branch-tree rows → per-checkpoint checkout-action specs (pure).

    Each spec drives one ``cl.Action`` in the web picker::

        {"seq": int, "label": str, "branch_id": <id>, "is_active": bool}

    ``label`` = ``#<seq> · <kind>[ · <anchor>][ (fork)]`` — the anchor is the
    #1547 preview; an inactive (dead-branch) node is tagged ``(fork)`` so the
    operator sees a checkout there is a fork-switch (vs an active-branch undo).
    Only checkpoint rows become actions; header rows are decorators.
    """
    rows = build_branch_tree_rows(branches, checkpoints)
    active_by_branch = {
        r["branch_id"]: bool(r.get("is_active"))
        for r in rows
        if r.get("row") != ROW_CHECKPOINT
    }
    specs: list[dict] = []
    for r in rows:
        if r.get("row") != ROW_CHECKPOINT:
            continue
        seq = r["seq"]
        kind = r.get("kind", "")
        anchor = r.get("anchor", "")
        is_active = active_by_branch.get(r.get("branch_id"), True)
        label = f"#{seq} · {kind}"
        if anchor:
            label += f" · {anchor}"
        if not is_active:
            label += "  (fork)"
        specs.append({
            "seq": seq,
            "label": label,
            "branch_id": r.get("branch_id"),
            "is_active": is_active,
        })
    return specs


async def handle_rewind_checkout(registry, seq: int) -> str:
    """Checkout to ``seq`` (the unified primitive: active = undo, dead-branch =
    fork-switch) and return a confirmation line for the web surface.

    Mirrors the TUI ``_do_checkout`` breadcrumb. Errors surface their reason
    (retention / unknown seq) as a message rather than raising into the glue.
    """
    if registry is None:
        return "⏪ checkout unavailable (no registry)"
    if seq is None:
        return "⏪ checkout unavailable (no seq in action)"
    try:
        result = await registry.checkout(seq)
    except Exception as exc:  # noqa: BLE001 — surface the reason to the user
        return f"⏪ checkout failed: {exc}"
    agents = result.get("agents", [])
    return (
        f"⏪ checked out to seq {result.get('target_n', seq)} "
        f"· {len(agents)} agent(s) reset · in-flight cancelled"
    )


__all__ = ["build_rewind_action_specs", "handle_rewind_checkout"]
