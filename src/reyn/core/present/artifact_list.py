"""#4482 PR-3: derive the "open a generated artifact" list from the SAME
message source the conversation pane itself renders — never a second,
persisted registry.

**Invariant 3 (lead-coder, #4440's dispatch, repeated verbatim across
three separate messages this session — the recurring class this arc is
named after):** this module walks `msg.meta["nodes"]` off the entries the
conversation view ALREADY holds, never `Session.history`'s own resident
buffer. `Session.history` has its own independent eviction axis
(`#4387`'s `history_resident` byte cap, `#4468`'s eviction) — an artifact
presented early in a long session can be evicted from `self.history`
while STILL visible in the conversation pane (FlowView keeps its own
entries; the two are not the same list, nor evicted on the same
schedule). Deriving the list from `self.history` would silently drop
"old but still-visible" artifacts from the list while the user can see
them right there on screen — a resource-management axis (what a memory
cap chose to keep) deciding a SEMANTIC question (what the user can open)
that has nothing to do with memory pressure. This is the third instance
of that exact shape flagged in one session (band lens: System Design —
responsibility at the wrong layer).

**No new persisted state** (owner/architect ruling): the list is derived
fresh from whatever the caller passes in (typically FlowView's own live
`entries`) — no separate on-disk index, no separate in-memory cache kept
warm across calls. A page of rows is only as expensive as the nodes on
that page: this module never walks the WHOLE session's history to build
one page, and never `stat()`s a file the caller does not ask about (see
:func:`stat_row` below, called only for rows about to be DISPLAYED, not
at collection time — "表示ページ分だけ stat").

Pure: no I/O in :func:`collect_artifact_rows` itself (a resolved
artifact-node payload is already fully self-describing — `ref`/`name`/
`media_type`/`body` — no filesystem access needed to list it). The one
I/O this module does (a single `os.stat`) is isolated in :func:`stat_row`,
called by the caller only for rows about to render."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactRow:
    """One listable artifact — everything needed to DISPLAY a row and to
    OPEN it, with no further derivation. `ref` is `None` for an inline
    (no-real-file) artifact — no OS file for :func:`~reyn.data.workspace.
    artifact_ref.resolve_ref` to open the ORDINARY way.

    `inline_content` (#4574 design B): a pure-inline row (`ref is None`,
    `is_inline` True) still carries its raw text here — #4574's own fix is
    that the CLIENT (never the agent, never an OS-side mint) materializes
    this to a temp file + opens it on request, rather than "nothing to
    open with the OS" being permanent. `None` for a ref-backed row (its
    content lives at `ref`, not duplicated here) or an error row.

    `resolved_path` (#4482 PR-3 review, lead-coder/architect — a real
    block, not a nit): `name` alone is a BASENAME — it names WHAT the
    artifact is, never WHERE it is, so two same-named artifacts in
    different directories are indistinguishable on the row alone. That
    fails the arc's one non-negotiable requirement ("ユーザが何を開こう
    としているか実体が見えること" — the user sees the REAL thing they're
    about to open) and architect's ruling ("表示から実行まで同じ path を
    使う"). `resolved_path` is set by :func:`resolve_display_paths` below
    — a project-root-relative path when the ref resolves, `None` when it
    doesn't (deleted, or an inline/error row with nothing to resolve) —
    and the display layer (`chrome.artifact_row_label`) prefers it over
    the bare `name`."""

    ref: "str | None"
    name: str
    media_type: "str | None"
    description: "str | None"
    is_inline: bool
    error: "str | None" = None
    resolved_path: "str | None" = None
    inline_content: "str | None" = None


def _artifact_row_from_node(node: dict) -> "ArtifactRow | None":
    """One `component == "artifact"` node -> its row, or `None` for a soft
    binding-miss (`{"component": "artifact"}` — nothing resolved, present's
    existing soft-skip philosophy; there is nothing displayable yet)."""
    if node.get("component") != "artifact":
        return None
    if "error" in node:
        return ArtifactRow(
            ref=None, name="", media_type=None, description=None,
            is_inline=False, error=str(node["error"]),
        )
    body = node.get("body")
    if not isinstance(body, dict):
        return None  # soft binding-miss — nothing resolved yet
    description = node.get("description")
    media_type = node.get("media_type")
    if "ref" in body:
        return ArtifactRow(
            ref=str(body["ref"]),
            name=str(node.get("name") or body["ref"]),
            media_type=media_type,
            description=description,
            is_inline=False,
        )
    # Inline body — no real file to point a ref at. #4574 design B: the raw
    # content is carried on the row so the CLIENT can materialize+open it on
    # request (never "nothing to open" permanently — see inline_content's
    # own docstring above).
    inline = body.get("inline")
    return ArtifactRow(
        ref=None,
        name=str(node.get("name") or "(inline)"),
        media_type=media_type,
        description=description,
        is_inline=True,
        inline_content=str(inline) if isinstance(inline, str) else None,
    )


def collect_artifact_rows(node_lists: "list[list[dict]]") -> list[ArtifactRow]:
    """Every artifact row across `node_lists` (one list per conversation
    message, in the SAME order the caller iterated its message source),
    newest-first.

    `node_lists` is plain data — a caller list-comprehends
    ``msg.meta.get("nodes", [])`` per entry (or per `presented`-event
    replay, off-loop) and hands the result here; this function never
    touches a live `FlowView`/`Session` itself, so it stays trivially
    testable with hand-built fixtures shaped like real resolved artifact
    payloads (see `artifact_payload.py`'s own docstring for that shape)."""
    rows: list[ArtifactRow] = []
    for nodes in node_lists:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            row = _artifact_row_from_node(node)
            if row is not None:
                rows.append(row)
    rows.reverse()  # newest-first
    return rows


def resolve_display_paths(
    rows: list[ArtifactRow], project_root: Path, agent_name: str,
) -> list[ArtifactRow]:
    """Fill in each ref-bearing row's `resolved_path` (#4482 PR-3 review —
    see :class:`ArtifactRow`'s own docstring for why `name` alone was not
    enough). The SAME resolution path :func:`~reyn.data.workspace.
    artifact_ref.resolve_ref` — the one `_handle_open_artifact_request`
    itself calls right before opening — never a second, independently-
    computed path, so what this shows and what actually opens cannot
    diverge.

    An inline row (`ref is None`) or an unresolvable one (deleted, or an
    unknown ref) passes through with `resolved_path` still `None` — the
    caller's own display layer falls back to `name` for those, which is
    the best available answer when there is genuinely nothing to resolve.

    The one piece of I/O this module's collection path does — called only
    on rows about to be DISPLAYED (a pane refresh), matching this module's
    own "no pre-stat, no persisted cache" discipline; see the module
    docstring."""
    from reyn.data.workspace.artifact_ref import resolve_ref

    out: list[ArtifactRow] = []
    for row in rows:
        if row.ref is None:
            out.append(row)
            continue
        resolved = resolve_ref(project_root, agent_name, row.ref)
        if resolved is None:
            out.append(row)
            continue
        try:
            display = str(resolved.relative_to(project_root))
        except ValueError:
            display = str(resolved)
        out.append(dataclasses.replace(row, resolved_path=display))
    return out


def stat_row(resolved_path: "Any | None") -> "int | None":
    """The ONE piece of I/O this module ever does — a single `os.stat`,
    called by the caller ONLY for a row about to be DISPLAYED (the
    "表示ページ分だけ stat" requirement), never at collection time. Returns
    the file's current size in bytes, or `None` if it can't be stat'd
    (deleted out from under the list — #4478's GC domain, not this
    module's; an unresolvable row simply shows as such, no crash)."""
    if resolved_path is None:
        return None
    try:
        return resolved_path.stat().st_size
    except OSError:
        return None
