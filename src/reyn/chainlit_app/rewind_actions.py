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

    ``editable`` = the row is a **turn** checkpoint → the glue also renders an
    edit (✎) action for it (ADR-0038 2d-3 decision A; non-turn rows get
    checkout-only). The genesis first-turn case is *not* filtered here (kept pure)
    — it is rejected on click by ``resolve_edit_target`` (decision B).
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
            "editable": kind == "turn",
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


def resolve_edit_target(registry, seq: int) -> dict:
    """Resolve the web edit-fork inputs for ``seq`` (ADR-0038 2d-3, chainlit-free).

    Returns ``{can_edit, original, fork_target, reason}``:

    - ``fork_target`` = ``predecessor_turn_checkpoint(seq)`` — the lineage-correct
      prior **turn** checkpoint (cross-fork-point + plan-step-skip). The edit
      re-runs from *before* the edited turn, so this is the checkout target.
    - ``original`` = ``anchor_store.get_full(seq)`` — the full original message to
      show in the prompt (Chainlit has no input pre-fill; the user retypes).
    - ``can_edit`` = False with a ``reason`` when there is no prior turn (genesis):
      the glue rejects the click rather than rendering a dead prompt (decision B —
      keeps the spec builder pure; matches the TUI 2c first-turn backstop).

    Mirrors the resolution half of TUI 2c ``_submit_edited_fork`` (app.py) — same
    substrate, surface-agnostic.
    """
    if registry is None:
        return {"can_edit": False, "original": "", "fork_target": None,
                "reason": "edit unavailable (no registry)"}
    fork_target = registry.predecessor_turn_checkpoint(seq)
    if fork_target is None:
        return {"can_edit": False, "original": "", "fork_target": None,
                "reason": "cannot edit the first turn — no earlier checkpoint to fork from"}
    anchors = registry.anchor_store
    original = anchors.get_full(seq) if anchors is not None else ""
    return {"can_edit": True, "original": original, "fork_target": fork_target,
            "reason": ""}


async def handle_rewind_edit_submit(registry, fork_target: int, edited: str) -> str:
    """Re-run an edited message from ``fork_target`` = a new fork (ADR-0038 2d-3).

    The 2c substrate path, surface-agnostic: ``checkout(fork_target)`` rewinds to
    the state before the edited turn, then ``submit_user_text(edited)`` re-runs the
    edited message — producing a sibling fork while the original turn stays on a
    now-inactive branch (append-only). Identical to TUI 2c ``_submit_edited_fork``
    (checkout(predecessor) → submit); only the prompt shell differs
    (cl.AskUserMessage vs InputBar edit-mode).

    **Submit MUST follow checkout (ordering invariant)**: ``checkout`` →
    ``reset_for_rewind`` *drains the session inbox* (session.py), so a message
    submitted *before* checkout would be discarded. We submit after. The session
    is mutated in-place (not reconstructed — ``_agents[name]`` is not reassigned),
    so ``attached_session()`` returns the same object a pre-call capture would;
    the re-fetch is defensive (and mirrors TUI 2c's ``_get_session()``-after-
    checkout), not identity-critical.

    Returns an empty string on success — the agent response flows through the
    existing repl_outbox → cl.Message drain. A checkout failure / missing
    collaborator surfaces its reason as a message rather than raising into the glue.
    """
    if registry is None or fork_target is None:
        return "⏪ edit unavailable"
    try:
        await registry.checkout(fork_target)
    except Exception as exc:  # noqa: BLE001 — surface the reason to the user
        return f"⏪ edit checkout failed: {exc}"
    session = registry.attached_session()
    if session is None:
        return "⏪ edit unavailable (no session)"
    await session.submit_user_text(edited)
    return ""


__all__ = [
    "build_rewind_action_specs",
    "handle_rewind_checkout",
    "resolve_edit_target",
    "handle_rewind_edit_submit",
]
