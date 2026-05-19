"""Pending tab — surfaces stalled / cross-channel operations.

Issue #277 (= Phase A TUI surface split from #268 / #270 umbrella).

Consumes ``list[PendingOpView]`` returned by
``ChatSession.list_stalled_interventions()``. Each row carries the
``kind`` discriminator (currently ``"intervention"`` only; future
``"mcp_call"`` / ``"peer_delegate"`` per #270 Phase B lift up).

Rendering dispatches on ``kind`` via ``_KIND_RENDERERS`` so future
kinds land by adding an entry to the table — TUI consume code path
doesn't churn as #270 expands the PendingOpView shape contract is
preserved (= consume-only, no shape changes from this layer per the
sub-issue scope guard).
"""
from __future__ import annotations

from typing import Any

from rich.text import Text as RichText

from .base import _CORAL


# ── kind-specific row renderers ──────────────────────────────────────────────
#
# Each renderer receives a ``PendingOpView``-shaped dict (= field
# names match the dataclass) and returns a list of Rich markup lines
# (one row + zero-or-more detail lines) to inject into the rendered
# output. The dispatch table at the bottom maps ``kind`` to renderer.
#
# Adding a future ``"mcp_call"`` kind = add one entry to the table +
# write its renderer; everything else (= cursor navigation,
# scroll-into-view, scope-guard for the empty-state) is generic.


def _render_kind_intervention(
    view: dict,
    *,
    is_cursor: bool,
) -> list[str]:
    """Renderer for ``kind="intervention"`` rows.

    Two lines per entry:
      ▶ <kind>   <short-id>   <origin>   <age>
        ↳ <summary>
    """
    pfx = f"[bold {_CORAL}]▶ [/]" if is_cursor else "  "
    name_style = f"bold {_CORAL}" if is_cursor else "#dddddd"

    iv_id_short = str(view.get("id", ""))[:8]
    origin = str(view.get("origin_channel_id", ""))
    age = _format_age(str(view.get("created_at", "")))
    summary = str(view.get("summary", ""))
    detail = str(view.get("detail", ""))

    head = (
        f"{pfx}[{name_style}]intervention[/]  "
        f"[#888888]{iv_id_short}[/]  "
        f"[#88aaff]{origin}[/]  "
        f"[#666666]{age}[/]"
    )
    lines = [head]
    if summary:
        lines.append(f"    [#666666]↳[/] [#aaaaaa]{summary[:60]}[/]")
    if detail:
        lines.append(f"      [#555555]{detail[:60]}[/]")
    return lines


def _render_kind_unknown(
    view: dict,
    *,
    is_cursor: bool,
) -> list[str]:
    """Fallback renderer for kinds the TUI doesn't recognize yet.

    Future ``mcp_call`` / ``peer_delegate`` will register their own
    entries, so this fallback only fires if a kind lands without
    a corresponding TUI update. Renders defensively to keep the tab
    legible even in that transitional state.
    """
    pfx = f"[bold {_CORAL}]▶ [/]" if is_cursor else "  "
    name_style = f"bold {_CORAL}" if is_cursor else "#dddddd"
    kind = str(view.get("kind", "?"))
    iv_id_short = str(view.get("id", ""))[:8]
    summary = str(view.get("summary", ""))
    return [
        f"{pfx}[{name_style}]{kind}[/]  [#888888]{iv_id_short}[/]  "
        f"[#aaaaaa]{summary[:60]}[/]",
    ]


# Future kinds add to this table — Phase B (= mcp_call / peer_delegate)
# expansion lands here without touching the generic frame.
_KIND_RENDERERS: dict[str, Any] = {
    "intervention": _render_kind_intervention,
}


def _format_age(created_at: str) -> str:
    """Approximate "Xs ago" / "Xm ago" / "Xh ago" / "Xd ago" from an ISO ts.

    Best-effort — when ``created_at`` is unparseable, returns the raw
    string. The age is just a soft signal, not load-bearing.
    """
    if not created_at:
        return ""
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(created_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (now - ts).total_seconds()
        if delta < 60:
            return f"{int(delta)}s"
        if delta < 3600:
            return f"{int(delta / 60)}m"
        if delta < 86400:
            return f"{int(delta / 3600)}h"
        return f"{int(delta / 86400)}d"
    except Exception:
        return created_at[:16]


def render_pending(
    pending_ops: list,
    *,
    cursor: int = 0,
    remote_mode: bool = False,
) -> tuple[str, list[dict], list[int]]:
    """Return ``(rendered_markup, flat_items, item_ys)`` for the Pending tab.

    ``pending_ops`` is a list of PendingOpView-shaped objects (= each
    can be a ``PendingOpView`` instance or a dict with the same field
    names — both flow through the renderer via attribute / key
    access). The renderer dispatches per-row on ``kind`` so future
    PendingOpView extensions add to ``_KIND_RENDERERS`` without
    touching the generic frame.

    ``remote_mode`` (= ``--connect`` mode integration, per #277 +
    #276 Gap #3 Phase C-(b)): when True, render a single "remote —
    limited" placeholder instead of attempting to fetch local data.
    The Pending tab's cross-channel observe / discard / claim
    operations require server-side state the WS client doesn't
    currently expose, so v1 takes the scoped-disable path. Phase C-(a)
    future iteration via REST API will lift this.

    ``flat_items`` mirrors agents / memory tabs — list of dicts with
    enough info for cursor navigation + slash command lookup. Each
    entry is the original PendingOpView field set plus a ``kind``
    discriminator.

    ``item_ys`` is the line index of each entry's primary row in
    the rendered output (= for scroll-into-view).
    """
    flat_items: list[dict] = []
    item_ys: list[int] = []

    if remote_mode:
        return (
            "[#aa6666]  remote — limited[/]\n"
            "[#666666]    Pending operations require local session state[/]\n"
            "[#666666]    (v1 ``--connect`` scoped disable per #277 / #276 Phase C-(b))[/]",
            flat_items,
            item_ys,
        )

    if not pending_ops:
        return (
            "[#555555]  No pending operations[/]\n"
            "[#555555]    (stalled / cross-channel ops surface here)[/]",
            flat_items,
            item_ys,
        )

    lines: list[str] = [
        f"[bold {_CORAL}]  Pending operations[/] "
        f"[#666666]({len(pending_ops)})[/]",
    ]

    for idx, view in enumerate(pending_ops):
        view_dict = _as_dict(view)
        kind = str(view_dict.get("kind", "?"))
        renderer = _KIND_RENDERERS.get(kind, _render_kind_unknown)
        is_cursor = (idx == cursor)

        # Record the y of this item's primary row (= first line of
        # the renderer output) before extending lines.
        item_ys.append(len(lines))

        rendered_lines = renderer(view_dict, is_cursor=is_cursor)
        lines.extend(rendered_lines)

        flat_items.append({
            "kind": kind,
            "id": view_dict.get("id", ""),
            "origin_channel_id": view_dict.get("origin_channel_id", ""),
            "created_at": view_dict.get("created_at", ""),
            "summary": view_dict.get("summary", ""),
            "detail": view_dict.get("detail", ""),
        })

    return "\n".join(lines), flat_items, item_ys


def _as_dict(view) -> dict:
    """Coerce a PendingOpView (dataclass) OR a dict into a dict.

    Tests + future shape changes may pass either; the renderer treats
    them uniformly via key access. Falls back to ``vars()`` for
    other object types (= duck-typing for future PendingOpView
    subclasses).
    """
    if isinstance(view, dict):
        return view
    try:
        return {
            "id": view.id,
            "kind": view.kind,
            "origin_channel_id": view.origin_channel_id,
            "created_at": view.created_at,
            "summary": view.summary,
            "detail": getattr(view, "detail", ""),
        }
    except AttributeError:
        try:
            return vars(view)
        except TypeError:
            return {}


__all__ = ["render_pending"]
