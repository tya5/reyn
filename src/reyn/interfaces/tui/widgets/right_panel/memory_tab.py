"""Memory tab — renders shared and per-agent memory entries with cursor."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .base import (
    _CORAL,
    _EVENT_SKILL,
    _EVENT_TOOL,
    _STATUS_SUCCESS,
    _TEXT_BODY,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MUTED,
    _TEXT_NEUTRAL,
    _esc,
)

_TYPE_COLORS: dict[str, str] = {
    "user":      _EVENT_SKILL,
    "feedback":  "#ffaa44",  # palette-candidate: feedback amber — no foundation token yet (near _EVENT_PLAN_STEP #ffaa66 but distinct)
    "project":   _STATUS_SUCCESS,
    "reference": _EVENT_TOOL,
}


_HOT_LIST_MAX_VISIBLE = 8


def _fmt_ago(last_ts: Any) -> str:
    """Best-effort relative-time string for the hot-list ``last_ts`` field.

    The ARS forwarder emits ``last_ts`` as a Unix-epoch float (see
    ``ActionUsageTracker.full_ranking``). Returns an empty string when
    the value is missing, unparseable, or in the future — callers append
    a dim suffix only when this returns non-empty so the layout stays
    intact for older payloads that omitted the field.
    """
    try:
        ts = float(last_ts)
    except (TypeError, ValueError):
        return ""
    delta = time.time() - ts
    if delta < 0 or ts <= 0:
        return ""
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def render_memory(
    project_root: Path | None,
    *,
    cursor: int = 0,
    hot_list: list[dict] | None = None,
    type_filter: str | None = None,
    embedding_state: dict | None = None,
) -> tuple[str, list[Any], list[int]]:
    """Return Rich markup + the flat ordered list of MemoryEntry items
    + the y-coordinate (= 0-indexed line number) of each entry's name row.

    The flat list lets the orchestrator drive cursor navigation and the
    Enter→preview integration without re-walking the disk. The row at
    index ``cursor`` is highlighted with a coral ▶ prefix.

    ``entry_ys[i]`` is the line-index of ``flat_entries[i]``'s name row in
    the rendered output. Section labels, per-type subheaders and blank
    separators all bump the y, so the orchestrator can't predict it
    arithmetically — we record it here, where the structure is known.

    ``hot_list`` (issue #192): the latest ARS qualified-name ranking
    from ``ChatLifecycleForwarder.on_hot_list_updated``. When non-empty,
    a "Hot now" sub-section renders above SHARED / AGENT scopes so the
    user can see why the router preferred skill X over Y on the last
    turn. The hot list is **not** part of ``flat_entries`` — entries
    listed there are MemoryEntry items, and the hot list carries
    qualified action names which are a different kind of object.

    ``embedding_state`` (FP-0043 C.4): the latest snapshot from the
    sentence-transformers lazy-load lifecycle, forwarded by
    ``ChatLifecycleForwarder.on_embedding_{status,skill_done,error}``.
    When present, an ``EMBEDDINGS`` end-section renders below SHARED /
    AGENT showing the current state — "loading…", "loaded · Nd", or
    "error · <retry hint>". The state is also NOT part of
    ``flat_entries`` (= metadata, not a navigable memory entry).
    """
    if project_root is None:
        return f"[{_TEXT_DIM}]  (no project root)[/]", [], []

    from reyn.data.memory.memory import list_entries

    lines: list[str] = []
    flat_entries: list[Any] = []
    entry_ys: list[int] = []
    # Wave-11 A#1 — memory-type filter banner. When type_filter is one
    # of the four known kinds, prepend a 2-row banner so the user has
    # a persistent cue that the list is narrowed (rather than the
    # missing types looking like "this scope has no FEEDBACK entries
    # actually"). Press [t] to cycle next; banner disappears when
    # back to None.
    if type_filter in _TYPE_COLORS:
        banner_color = _TYPE_COLORS[type_filter]
        lines.append(
            f"  [bold {banner_color}]⌕ filter: [/]"
            f"[bold {_CORAL}]{_esc(type_filter.upper())}[/]"
        )
        lines.append(
            f"  [{_TEXT_DIM}]  press [/][bold {_TEXT_BODY}]\\[t][/]"
            f"[{_TEXT_DIM}] to cycle filter[/]"
        )

    # Hot now section (issue #192). Always renders the header so the
    # feature is discoverable on cold-start (= before any router
    # activity). Wave-4 PC5: previously the entire section was
    # conditionally hidden when ``hot_list`` was empty, so first-launch
    # users never learned the section existed. Now the header is
    # always there + a dim placeholder line when no data, populated
    # rows once the ARS forwarder emits ``hot_list_updated``.
    # Capped at _HOT_LIST_MAX_VISIBLE so a long ranking doesn't push
    # the SHARED / AGENT entries off the top of a narrow panel.
    lines.append("[bold #ffaa44]  HOT NOW[/]")  # palette-candidate: feedback amber — no foundation token yet
    if hot_list:
        for entry in hot_list[:_HOT_LIST_MAX_VISIBLE]:
            try:
                name = str(entry.get("qualified_name", ""))
                freq = int(entry.get("freq", 0))
            except (AttributeError, ValueError, TypeError):
                continue
            if not name:
                continue
            if freq <= 0:
                continue
            ago = _fmt_ago(entry.get("last_ts") if hasattr(entry, "get") else None)
            ago_suffix = f"  [{_TEXT_DIM}]{ago}[/]" if ago else ""
            lines.append(
                f"[#ffaa44]    🔥 [/][{_TEXT_BRIGHT}]{_esc(name)}[/]  "  # palette-candidate: feedback amber — no foundation token yet
                f"[{_TEXT_NEUTRAL}]×{freq}[/]{ago_suffix}"
            )
        overflow = len(hot_list) - _HOT_LIST_MAX_VISIBLE
        if overflow > 0:
            lines.append(
                f"[{_TEXT_DIM}]    … {overflow} more[/]"
            )
    else:
        lines.append(f"[{_TEXT_DIM}]    (no router activity yet)[/]")
    lines.append("")

    def _render_scope(entries: list, label: str, label_color: str) -> None:
        lines.append(f"[bold {label_color}]  {_esc(label)}[/]")
        if not entries:
            # Two short lines instead of one long one — the previous
            # single line ``(empty — ask reyn to "remember <fact>")``
            # (45 cells incl. indent) clipped to ``(empty — ask reyn to
            # "re…`` at the default 33%-panel content width (~22 cells).
            # Splitting preserves both the "empty" signal and the
            # call-to-action and survives narrow panes.
            lines.append(f"[{_TEXT_DIM}]    (empty)[/]")
            lines.append(
                f"[{_TEXT_DIM}]    try: \"remember <fact>\"[/]"
            )
            lines.append("")
            return
        groups: dict[str, list] = {
            t: [] for t in ("user", "feedback", "project", "reference")
        }
        other: list = []
        for e in entries:
            if e.type in groups:
                groups[e.type].append(e)
            else:
                other.append(e)
        # Wave-11 A#1 — when type_filter is active, drop every type
        # except the one named. ``other`` is also wiped because the
        # filter contract is "show ONLY this type". Sub-headers for
        # skipped types vanish too because the ``if not group``
        # guard below skips empty buckets.
        if type_filter in groups:
            for k in list(groups.keys()):
                if k != type_filter:
                    groups[k] = []
            other = []
        for type_key in ("user", "feedback", "project", "reference"):
            group = groups[type_key]
            if not group:
                continue
            color = _TYPE_COLORS[type_key]
            lines.append(f"[bold {color}]    \\[{type_key.upper()}][/]")
            for e in group:
                flat_entries.append(e)
                entry_ys.append(len(lines))
                is_cursor = (len(flat_entries) - 1) == cursor
                indent = f"[bold {_CORAL}]    ▶ [/]" if is_cursor else "      "
                name_style = f"bold {_CORAL}" if is_cursor else _TEXT_BRIGHT
                lines.append(f"{indent}[{name_style}]{_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[{_TEXT_DIM}]        {_esc(e.description)}[/]")
        if other:
            lines.append(f"[bold {_TEXT_MUTED}]    \\[OTHER][/]")
            for e in other:
                flat_entries.append(e)
                entry_ys.append(len(lines))
                is_cursor = (len(flat_entries) - 1) == cursor
                indent = f"[bold {_CORAL}]    ▶ [/]" if is_cursor else "      "
                name_style = f"bold {_CORAL}" if is_cursor else _TEXT_BRIGHT
                lines.append(f"{indent}[{name_style}]{_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[{_TEXT_DIM}]        {_esc(e.description)}[/]")
        lines.append("")

    # Shared memory
    shared = list_entries(project_root / ".reyn" / "memory")
    _render_scope(shared, "SHARED", _CORAL)

    # Per-agent memory
    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.exists():
        for agent_dir in sorted(agents_dir.iterdir()):
            mem_dir = agent_dir / "memory"
            if not mem_dir.exists():
                continue
            agent_entries = list_entries(mem_dir)
            _render_scope(agent_entries, f"AGENT  {agent_dir.name}", "#7a9fc7")

    # FP-0043 C.4 — Embedding model-load lifecycle section. Renders
    # only when an event has been observed this session; absent state
    # means "operator hasn't enabled embedding_class" (= the §C.1
    # list_actions hint covers that path on the LLM side), so the TUI
    # stays quiet rather than displaying a permanent "(none)" line.
    if embedding_state:
        _render_embedding_section(lines, embedding_state)

    return "\n".join(lines), flat_entries, entry_ys


def _render_embedding_section(
    lines: list[str], state: dict,
) -> None:
    """Append the EMBEDDINGS section reflecting the latest lifecycle event.

    Three shapes per ``state["kind"]``:

      embedding_status     loading… · <model> · <device>
      embedding_skill_done loaded · <model> · <dimension>d
      embedding_error      error · <model> · <retry_hint truncated>

    The model string is shortened (= drop the ``sentence-transformers/``
    prefix when present) so it fits a narrow right panel.
    """
    kind = str(state.get("kind", "") or "")
    model_raw = str(state.get("model", "") or "")
    model = model_raw[len("sentence-transformers/"):] if model_raw.startswith(
        "sentence-transformers/",
    ) else model_raw

    lines.append("[bold #6aa9c7]  EMBEDDINGS[/]")
    if kind == "embedding_status":
        device = str(state.get("device", "") or "")
        device_part = f" · {_esc(device)}" if device else ""
        model_part = f" · {_esc(model)}" if model else ""
        lines.append(
            f"[{_TEXT_MUTED}]    ⟳ loading…[/][{_TEXT_BODY}]{model_part}{device_part}[/]"
        )
    elif kind == "embedding_skill_done":
        try:
            dim = int(state.get("dimension", 0))
        except (TypeError, ValueError):
            dim = 0
        dim_part = f" · {dim}d" if dim > 0 else ""
        model_part = f" · {_esc(model)}" if model else ""
        lines.append(
            f"[{_STATUS_SUCCESS}]    ✓ loaded[/][#cccccc]{model_part}{dim_part}[/]"  # palette-candidate: near-bright — no foundation token yet
        )
    elif kind == "embedding_error":
        hint_raw = str(state.get("retry_hint", "") or "")
        # Truncate the retry hint so it survives the ~33%-panel
        # default width; the full text is in the events tab.
        max_hint = 60
        hint = hint_raw if len(hint_raw) <= max_hint else hint_raw[:max_hint - 1].rstrip() + "…"
        model_part = f" · {_esc(model)}" if model else ""
        lines.append(
            f"[#ff6666]    ✗ error[/][{_TEXT_BRIGHT}]{model_part}[/]"  # palette-candidate: embedding error red — no foundation token yet (distinct from _STATUS_ERROR #ff6644)
        )
        if hint:
            lines.append(f"[{_TEXT_BODY}]      {_esc(hint)}[/]")
    else:
        # Unknown kind from a future emitter — show the raw text as a
        # graceful fallback rather than rendering nothing at all.
        text = str(state.get("text", "") or "(no detail)")
        lines.append(f"[{_TEXT_MUTED}]    {_esc(text)}[/]")
    lines.append("")


__all__ = ["render_memory"]
