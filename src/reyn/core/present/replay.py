"""Replay/rewind re-render of a ``presented`` event (FP-0054 PR-D, §8).

A presentation is a **cache**; the ``presented`` P6 event is the **truth**. When a
session is replayed (``reyn events <log>`` re-renders a saved log) or rewound, a
``presented`` event is re-rendered **best-effort from the event** — never a crash,
never a stale render:

- The event carries **refs + stats only, never content bytes** (audit-first, §7). So a
  best-effort re-render must re-read the ``data_ref`` from disk and re-synthesize a
  view of it. It does NOT (and cannot) reconstruct the caller's original inline
  blueprint — the event stores only a name / hash of it — so the re-render uses the
  §3 stage-3/stage-4 default viewer chain (the same generic path the live op falls back
  to), which always renders from the data's shape.
- When the ``data_ref`` is **gone** (GC'd / unavailable) — or the presentation was of
  ``data_inline`` whose bytes were never persisted (only its ``<inline-data>`` marker
  is in the event) — the re-render is an **expiry placeholder** that points at the
  durable ``presented`` audit event, so the reader still knows a presentation happened
  and where its record lives.

**Display-only — no reconstructed state (recovery gate N/A, §8).** This module derives
NOTHING authoritative from ``presented`` events: it produces a projection for display
(re-render or placeholder) from a durable event + an already-durable ref, and writes no
recovery-core state. The CLAUDE.md truncate-falsify recovery-feature gate therefore does
not apply. If a future revision ever reconstructs authoritative state from ``presented``
events, that PR must carry the truncate-falsify test in-arc.

The re-render is surface-agnostic here (plain text lines); a surface (the inline-CUI
console replay via ``reporters.ConsoleLogger.on_presented``) prints them. ``load_ref`` is
injectable so the re-render is exercisable with a real function (no mock) and so a rewind
surface can supply its own ref access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from reyn.core.present.binding import ResolvedPresentation, resolve_bindings
from reyn.core.present.catalog import validate_blueprint
from reyn.core.present.fallback import default_viewer_blueprint, generic_blueprint
from reyn.core.present.source import rehydrate_ref_text
from reyn.data.workspace.text_codec import decode_text_or_none

# The ``presented`` event's ``data_ref`` for a ``data_inline`` presentation — the bytes
# were in the LLM's context, never persisted, so replay cannot re-render them.
_INLINE_MARKER = "<inline-data>"

#: A ``data_ref`` → re-hydrated value, or ``None`` when the ref is gone / unreadable.
RefLoader = Callable[[str], Optional[Any]]


@dataclass
class ReplayedPresentation:
    """The best-effort re-render of one ``presented`` event, for display on replay.

    ``lines`` is either the best-effort re-rendered content (``is_placeholder=False``)
    or the expiry-placeholder text pointing at the audit event
    (``is_placeholder=True``). ``header`` is a one-line summary of the recorded audit
    stats. Never raises to the caller — a gone ref becomes a placeholder, not an error.
    """

    data_ref: str
    view: str
    header: str
    lines: list[str] = field(default_factory=list)
    is_placeholder: bool = False


def load_ref_from_disk(data_ref: str) -> "Any | None":
    """Default :data:`RefLoader`: re-hydrate a ``data_ref`` from disk, or ``None`` when
    it is gone / unreadable. Mirrors ``resolve_present_source``'s re-hydration (offloaded
    structured JSON → object; plain text → string) but without an authority gate — replay
    is an offline projection of an already-audited read, not a fresh file op."""
    path = Path(data_ref)
    try:
        if not path.is_file():
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    text, _encoding = decode_text_or_none(raw)
    if text is None:
        return {"binary": True, "byte_size": len(raw)}
    return rehydrate_ref_text(text)


def _surface_of(event_data: dict) -> str:
    """The recorded surface (``surface`` is a list in the event, e.g. ``["inline-cui"]``)
    so the guard's per-surface neutralizer matches the sink the data reaches on replay;
    ``"null"`` (plain) when unrecorded."""
    surface = event_data.get("surface")
    if isinstance(surface, list) and surface:
        return str(surface[0])
    if isinstance(surface, str) and surface:
        return surface
    return "null"


def _best_effort_resolved(value: Any, surface: str) -> ResolvedPresentation:
    """Re-render ``value`` through the §3 stage-3 (content-type default viewer) → stage-4
    (generic YAML/text, always renders) chain — the same generic path the live op falls
    back to. The original inline blueprint is not in the event, so this is the faithful
    best-effort view of the data's shape."""
    stage3 = resolve_bindings(
        validate_blueprint(default_viewer_blueprint(value)), value, surface=surface,
    )
    if not stage3.all_bindings_missed:
        return stage3
    return resolve_bindings(
        validate_blueprint(generic_blueprint(value)), value, surface=surface,
    )


def _flatten_nodes(nodes: list[dict]) -> list[str]:
    """Flatten a resolved render model to plain text lines for a console replay surface
    (surface-agnostic; a Rich surface would render the nodes directly instead)."""
    lines: list[str] = []
    for node in nodes:
        component = node.get("component")
        if component in {"text", "markdown", "code", "diff"}:
            lines.extend(node.get("text", "").splitlines() or [""])
        elif component == "keyvalue":
            for row in node.get("rows", []):
                lines.append(f"{row.get('label', '')}: {row.get('value', '')}")
        elif component == "table":
            columns = node.get("columns", [])
            lines.append(" | ".join(col.get("header", "") for col in columns))
            n_rows = max((len(col.get("cells", [])) for col in columns), default=0)
            for i in range(n_rows):
                lines.append(" | ".join(
                    col["cells"][i] if i < len(col.get("cells", [])) else ""
                    for col in columns
                ))
        elif component == "list":
            lines.extend(f"• {item}" for item in node.get("items", []))
        elif component == "image":
            lines.append(f"[image: {node.get('alt') or node.get('src') or ''}]")
    return lines


def _header(event_data: dict, view: str, data_ref: str) -> str:
    rows = event_data.get("rows", 0)
    resolved = event_data.get("bindings_resolved", 0)
    dropped = event_data.get("bindings_dropped") or []
    drop_note = f", {len(dropped)} dropped" if dropped else ""
    return (
        f"[present] view={view} data_ref={data_ref} "
        f"rows={rows} bindings_resolved={resolved}{drop_note}"
    )


def _placeholder_lines(data_ref: str, view: str, event_data: dict, *, inline: bool) -> list[str]:
    """Expiry placeholder pointing at the durable ``presented`` audit event."""
    rows = event_data.get("rows", 0)
    if inline:
        reason = "inline data was never persisted (only its audit record was)"
    else:
        reason = f"the presented data is no longer available (ref: {data_ref})"
    return [
        f"[present · expired] {reason}.",
        (
            "  This is a best-effort cache re-render; the durable record is the "
            f"`presented` audit event (view={view}, rows={rows}). "
            "Re-run the agent to regenerate the presentation."
        ),
    ]


def replay_presentation(
    event_data: dict, *, load_ref: RefLoader = load_ref_from_disk,
) -> ReplayedPresentation:
    """Re-render one ``presented`` event best-effort for replay/rewind display.

    ``event_data`` is the ``presented`` event's ``data`` dict (``data_ref``,
    ``view``, ``surface``, ``rows``, ``bindings_resolved``, ``bindings_dropped``).
    ``load_ref`` re-hydrates the ref (default: from disk); returning ``None`` — or an
    ``<inline-data>`` ref, whose bytes the event never carried — yields an expiry
    placeholder pointing at the audit event. Never raises: a gone ref is a placeholder,
    not a crash, and a re-render is never a stale render (it re-reads the current ref).
    """
    data_ref = str(event_data.get("data_ref", "<unknown>"))
    view = str(event_data.get("view", "<unknown>"))
    header = _header(event_data, view, data_ref)

    if data_ref == _INLINE_MARKER:
        return ReplayedPresentation(
            data_ref=data_ref, view=view, header=header,
            lines=_placeholder_lines(data_ref, view, event_data, inline=True),
            is_placeholder=True,
        )

    value = load_ref(data_ref)
    if value is None:
        return ReplayedPresentation(
            data_ref=data_ref, view=view, header=header,
            lines=_placeholder_lines(data_ref, view, event_data, inline=False),
            is_placeholder=True,
        )

    resolved = _best_effort_resolved(value, _surface_of(event_data))
    return ReplayedPresentation(
        data_ref=data_ref, view=view, header=header,
        lines=_flatten_nodes(resolved.nodes), is_placeholder=False,
    )
