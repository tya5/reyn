"""#4482 PR-1: ref -> path table for LLM-produced artifacts the terminal
can't render natively (html/office/pdf/images) that a user opens with the
OS's own default app — one step beyond #383/#385's tool-result spill
mechanism, which only ever minted a ref for something explicitly saved as
a *tool result*. A present-node artifact can be any file the agent already
produced anywhere in the project (e.g. via ``write_file``), so this is a
separate, general casting mechanism, not a MediaStore extension.

**Ref identity = (agent, absolute path), 1:1** (architect's #4482 ruling).
A content-hash identity was considered and rejected: it conflicts directly
with the owner's own "don't copy, open the original" decision — a
content-hash ref can only stay resolvable across a content change by
capturing the bytes at mint time, which IS a copy. Path identity instead
means a file regenerated at the same path resolves to TODAY's bytes under
the SAME ref — correct, matching the meaning a file manager / editor /
nvim's own ``gx`` already give the same shape ("report.pptx" a later turn
re-creates still means "the current report.pptx", not a stale snapshot).

**Minting is idempotent**: the SAME ``(agent, normalized path)`` pair always
returns the SAME ref — no re-numbering, no duplicate table entries.
Normalization goes through :func:`normalize_ref_path` (#4482 PR-1's own
independent slice, landed as #4495) — :func:`mint_ref` and
:func:`resolve_ref` both call it, never a locally re-derived variant
(architect's review: splitting normalization across two call sites is how
the SAME file ends up minting two different refs).

**Scope is per-agent** ("session" in architect's own wording — the fetch
endpoint this feeds is already agent-scoped, ``/agents/{agent}/...``, so
there is no cross-agent need and no extra cleanup surface to invent).

**Table persisted under ``.reyn/memory/`` — PERSIST tier, not
``.reyn/cache/``** (#4584 fix; originally mirrored #4432's tool-result
spill manifest's ``cache/`` home, which turned out to be the wrong
precedent for BOTH tables to follow — #4584 moved that one too). ``cache/``
is documented "DERIVED — rebuilt after restore" (``reyn-dir-layout.md``) —
never true for this table: the (agent, path) → ref mapping exists ONLY at
the instant :func:`mint_ref` writes it — no WAL event, no
``ChatMessage``/conversation-log entry, no other durable record anywhere
carries it (measured directly, #4494/#4584: a ``present`` tool call's own
tool-result carries only ack STATISTICS, never the resolved ``ref``). An
operator correctly deleting the table because the doc calls it
safely-rebuildable, but that in fact cannot be rebuilt, silently kills
every past ``/open`` — the #4584 defect this move closes.

``reyn-dir-layout.md``'s own persist-tier decision rule ("knowledge /
decision that must SURVIVE rewind → ``memory/``") is the only rule this
table's shape actually satisfies among the doc's existing tiers — "memory"
as a NAME is an imperfect fit (a ref→path table is neither knowledge nor a
decision), recorded rather than silently accepted; introducing a sibling
tier for this one table was judged out of scope for #4584 (lead-coder/
architect co-vet, 2026-08-13).

Same JSONL-append shape, same best-effort write-failure tolerance as
before (a write failure here must never fail the mint itself — the ref is
still valid for this process's lifetime even if a LATER process's table
load won't see it) — only the TIER moved, not the mechanism. **The move
does NOT change write permissions**: ``security/permissions/file_scope.py``'s
``ZoneStateDir`` carves out only ``.reyn/config/`` + ``.reyn/state/`` +
``approvals.yaml`` as the write-gated recovery-core surface — ``memory/``
is an ordinary agent-writable zone, identically to ``cache/``.

**Never copies bytes.** :func:`mint_ref` never reads the file's content,
only records its path; :func:`resolve_ref` hands back a path for the
CALLER to open or serve — the same bytes, never a second copy (doubling
disk usage is exactly what #4478's own GC precondition would have to
measure against, so this module must not create that pressure itself).

Explicitly NOT this module's job (lead-coder's #4482 PR-1 brief):
  - "has the file changed since it was presented" — a client-side
    mtime-vs-seq comparison (PR-3's own scope); no new persisted state
    lives here.
  - GC / retention — #4478's own domain. An unresolvable ref (file
    deleted out from under it) is simply :func:`resolve_ref` returning
    ``None``; distinguishing "deleted" from "content changed" is handled
    by NOT treating a still-existing-but-different-bytes file as
    unresolvable at all — only an actually-missing file returns ``None``.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from reyn.data.workspace.ref_path_normalize import normalize_ref_path

_REF_TABLE_FILENAME = "artifact_refs.jsonl"


def _table_path(project_root: Path) -> Path:
    # #4584: PERSIST tier — under `.reyn/memory/`, mirroring the doc's own
    # decision rule for "must survive rewind" data (see this module's own
    # docstring for why `memory/` as a NAME is an imperfect but chosen fit).
    return project_root / ".reyn" / "memory" / _REF_TABLE_FILENAME


def _load_table(project_root: Path) -> "list[dict]":
    path = _table_path(project_root)
    if not path.is_file():
        return []
    entries: "list[dict]" = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # one malformed line never invalidates the rest
    except OSError:
        return []
    return entries


def mint_ref(project_root: Path, agent_name: str, path: "str | Path") -> str:
    """Return the ref for *path* under *agent_name*'s scope.

    Idempotent: a (agent, normalized-path) pair that already has an entry
    returns that SAME ref rather than minting a new one — the acceptance
    criterion this exists to satisfy. *path* is normalized via
    :func:`normalize_ref_path` before either the lookup or the new-entry
    write, so a relative spelling, a symlink, or (on a case-insensitive
    filesystem) a different-case spelling of an already-minted path never
    produces a second ref for the same file.

    Never reads *path*'s content — only records where it is. A write
    failure to the on-disk table is swallowed (best-effort, matching
    #4432's own spill-manifest tolerance): the freshly minted ref is still
    returned and usable for the rest of this process's lifetime even if a
    later process's table load won't see it.
    """
    normalized = normalize_ref_path(path, project_root)
    entries = _load_table(project_root)
    for entry in entries:
        if entry.get("agent") == agent_name and entry.get("path") == str(normalized):
            return entry["ref"]

    ref = secrets.token_urlsafe(9)
    table_path = _table_path(project_root)
    try:
        table_path.parent.mkdir(parents=True, exist_ok=True)
        with table_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"ref": ref, "agent": agent_name, "path": str(normalized)}) + "\n",
            )
    except OSError:
        pass
    return ref


def resolve_ref(project_root: Path, agent_name: str, ref: str) -> "Path | None":
    """Resolve *ref* (minted under *agent_name*'s scope) back to its
    absolute path.

    Returns ``None`` when the ref is unknown to this agent's scope, OR
    when the file it names no longer exists on disk — an unresolvable ref
    is this function's ENTIRE answer to "the artifact was deleted"; #4478's
    own GC, if it ever runs, needs no separate hook here (see the module
    docstring). A file that still exists but whose CONTENT has changed
    since minting is NOT unresolvable — it resolves normally, to today's
    bytes, which is the whole point of path (not content-hash) identity.
    """
    entries = _load_table(project_root)
    for entry in entries:
        if entry.get("agent") == agent_name and entry.get("ref") == ref:
            candidate = Path(entry["path"])
            return candidate if candidate.exists() else None
    return None


def list_refs_for_agent(
    project_root: Path, agent_name: str, *, limit: "int | None" = None,
) -> "tuple[list[dict], int]":
    """#4494 design C: every ``{"ref", "path"}`` entry minted under
    *agent_name*'s scope, newest-first — the durable source a REMOTE
    client's own artifact list falls back to when its live conversation
    view carries nothing (frame-sufficiency: past turns are not on the
    wire — the SAME gap also affects a LOCAL client right after a
    restart, since ``restore.project_restored_frames`` has no
    "presentation" kind reconstruction either, #4584's own measured
    finding — this function is transport-agnostic and serves both).

    **#4601: the ONE fallback join point both in-repo callers
    (``InProcessTransport.request_artifact_list`` and the AG-UI
    endpoint's ``artifact_list_request`` handler) share** — the table is
    append-only and persist-tier (#4584), so it never shrinks; capping
    HERE (rather than in each caller separately) is what keeps both
    transports bounded together, per architect's own #4601 finding that
    a cap placed only at the endpoint would leave the TUI's identical
    fallback path unbounded. ``limit`` truncates newest-first (so a
    truncated list is always the N MOST RECENT artifacts) — pass
    ``None`` for no cap (existing/internal callers, tests).

    Returns ``(entries, total)`` — ``total`` is the FULL matching count
    before truncation, so a caller can disclose "newest N of M" rather
    than silently dropping the tail (owner's standing instruction: no
    baseless cap without visibility into what it cut).

    Deliberately raw entries, not :class:`~reyn.core.present.artifact_list.
    ArtifactRow` objects — this module has no ``media_type``/
    ``description`` to offer (the table was never designed to carry them;
    only :func:`mint_ref`'s two fields exist), and no existence check
    (mirrors :func:`~reyn.core.present.artifact_list.collect_artifact_rows`'s
    own "stat only what's about to be displayed" discipline — the CALLER
    decides how many rows it is about to render before paying that I/O;
    #4601 architect ruling: that decision now happens in THIS order —
    truncate first, stat only the N survivors — see
    :func:`~reyn.core.present.artifact_list.resolve_display_paths`'s own
    callers)."""
    entries = [e for e in _load_table(project_root) if e.get("agent") == agent_name]
    entries.reverse()  # newest-first, matching collect_artifact_rows' own convention
    rows = [{"ref": e["ref"], "path": e["path"]} for e in entries if "ref" in e and "path" in e]
    total = len(rows)
    if limit is not None:
        rows = rows[:limit]
    return rows, total
