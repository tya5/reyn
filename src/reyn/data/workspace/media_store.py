"""MediaStore — flat-file storage for multimodal media + tool result text
under ``.reyn/`` (issue #383 E-full Phase 3, F1-B scope).

Two storage directories with parallel file naming convention:

  .reyn/media/         — image binary (= web_fetch image / file_read image /
                         mcp image media blocks). Consumed by the chat
                         router and history builder.
  .reyn/tool-results/  — text-y tool result dumps (= web_fetch text /
                         mcp text / future preview-driven tool results
                         per #385). PR-C lands the writer; PR-D wires
                         the consumer + preview generation.

Filename convention (both dirs):

  <YYYYMMDDTHHMMSS>-<chain_short>-<tool>-<seq>.<ext>

This sorts chronologically with ``ls -la``, groups by conversation chain
when you grep for ``<chain_short>``, and is browseable as plain files —
users can ``open``, ``ls``, or delete entries to manage disk usage.

ChatMessage carries **path-refs** (= ``{"type": "image", "path": ...,
"mime_type": ..., "content_hash": ...}``) instead of inline base64. The
LLM-wire boundary (``_build_history_for_router`` / the chat router's
synthetic follow-up builder) reads the path, encodes, and embeds the
binary as a data URL ONLY when sending to the model. Storage stays
light; the LLM sees the materialised form.

Lifecycle policy (#385 β core impl sub-task 5, 2026-05-22 frozen
contract Phase 1 = "(a) Persistent until user delete"):

  - **No auto-GC.** Files written by ``save_*`` remain on disk until a
    user / operator deletes them out-of-band (= ``rm``, file explorer,
    cleanup script). The MediaStore class does NOT enforce TTL,
    max-N, session-end cleanup, or any other automatic eviction.
  - **Cross-turn / cross-session re-access supported.** A path-ref
    minted in user turn 1 remains valid in user turn 2 / next chat
    session / a forwarded A2A peer's expand — the file is still there.
    (See Q1 of the frozen contract: ``agent_id = agent name`` durable
    identity, not per-turn ``chain_id``.)
  - **Disk usage grows unboundedly.** Documented operational caveat —
    operators are expected to ``ls -la .reyn/tool-results/`` and clean
    up periodically. The browsable filename convention makes manual
    audit straightforward. :meth:`MediaStore.storage_stats` (#4478
    Phase 1) gives a scripted read of this instead of a manual ``ls``:
    file counts + byte totals per directory, policy-independent — it
    exists to SUPPLY the measurement evidence Phase 2 is gated on, it
    does not itself decide anything.
  - **Phase 2 reservation.** When measurement surfaces a real disk
    pressure or stale-handle problem (= not just hypothetical), Phase 2
    adds a config-driven policy: TTL / LRU / session-end / mixed. The
    reyn.yaml ``multimodal:`` block is the natural insertion point;
    no schema reservation made today (= YAGNI). The ``MediaStoreConfig``
    dataclass is the future extension surface.

Out of scope (= future work):
  - Phase 2 cleanup policy (= TTL / max-N / session boundary). Trigger
    is measurement evidence, not hypothesis — #4478 lands the
    measurement (``storage_stats``) without the policy. A future Phase
    2's own acceptance criteria MUST cover the user-visible side, not
    just "no consumer crashes": every read consumer already tolerates
    a missing file (404 / ``None`` / a silently dropped content block —
    #4478's own consumer sweep confirmed this), but a silently dropped
    block is a scrollback image or tool-result the USER sees vanish
    with no explanation, not a safe no-op. "Does this crash" is not
    "is this a good experience" — Phase 2 needs to answer the second
    question too, not just the first.
  - Cross-host RPC dispatcher for ``resource_uri`` (= #385 β core
    impl sub-task 3, pending scheme arbitration).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.services.offload.store import offload_value, read_offloaded

if TYPE_CHECKING:
    from reyn.core.events.durability_worker import DurabilityWorker

logger = logging.getLogger(__name__)

#: #4381: one JSON object per line (``{"path": "<absolute path>"}``), one
#: line per :meth:`MediaStore.save_tool_result` write — the persisted
#: cross-process spill-provenance manifest.
#:
#: Lives under ``.reyn/memory/`` — PERSIST tier (#4584 fix; previously
#: ``.reyn/cache/``, moved there from ``tool_results_dir`` by #4432 round
#: 3's own review). NOT under ``tool_results_dir``: every file
#: ``tool_results_dir`` holds is written through this exact path
#: (``offload_value`` in :meth:`save_tool_result` is its ONLY writer), so a
#: consumer that lists that directory and expects "N spill artifacts" is
#: entitled to get exactly N — mixing the ledger into the same namespace
#: broke that (``tests/runtime/test_2425_step1c_chat_chokepoint.py``'s
#: ``sorted(store.tool_results_dir.iterdir())`` unpack). ``.reyn/tool-
#: results/`` is also classified *audit* (append-only forensic record,
#: never read back) in ``reyn-dir-layout.md`` — this manifest IS read back,
#: every process start, so it does not belong in an audit-only namespace
#: either.
#:
#: **Why NOT ``cache/`` (#4584 correction of the #4432-era reasoning
#: above):** this module's own comment already said, verbatim, "this
#: manifest is not literally rebuildable from other state the way that
#: [``budget_checkpoint.json``] ledger-derived total is" — i.e. it was
#: filed under *derived* while explicitly acknowledging it isn't derived.
#: #4584 measured directly (``artifact_ref.py``'s sibling table hit the
#: identical shape): no rebuild/reconstruction code path exists anywhere
#: for this manifest — an operator following ``reyn-dir-layout.md``'s
#: (now-corrected) "cache/ is derived, safe to clean up" classification
#: would silently break every already-spilled tool-result reference this
#: manifest is the ONLY record of. *Persist* (survives rewind, never
#: reverted, ``reyn-dir-layout.md``'s own "must SURVIVE rewind → memory/"
#: rule — the same landing spot ``artifact_ref.py``'s sibling table uses,
#: same "memory as a NAME is an imperfect fit" caveat) is the honest tier:
#: the entries here were never expected to be reconstructed, only ever
#: written once and read back.
#:
#: **The move does NOT change write permissions** — a real, separate axis
#: from tier classification, worth stating explicitly since a reader could
#: otherwise assume "persist" means "harder to write to". ``security/
#: permissions/file_scope.py``'s ``ZoneStateDir`` carves out ONLY
#: ``.reyn/config/`` + ``.reyn/state/`` + ``approvals.yaml`` as the
#: write-gated recovery-core surface (confirmed by reading
#: ``_RECOVERY_CORE_WRITE_PREFIXES`` / ``_CANONICAL_PROTECTED_WRITE_PATHS``
#: in ``security/permissions/permissions.py``, not assumed) — ``memory/``
#: is an ordinary agent-writable zone, identically to ``cache/``. A
#: safe-mode ``file.write`` could still inject or erase a line here before
#: or after this move, exactly as it always could under ``cache/``.
#: Closing THAT requires ``.reyn/state/`` behind a dedicated WAL-emitting
#: op — a bigger design call, deliberately not made in this fixup (same
#: "flag, don't silently half-fix" call as this PR's own
#: ``MAX_CONTROL_IR_RESULT_INLINE_BYTES`` note).
_SPILL_MANIFEST_FILENAME = "tool_result_spills.jsonl"

# Conservative mapping from MIME type to file extension; unknown types
# fall back to ``""`` so the storage layer still writes a file (= user
# can rename / inspect with their preferred tool). Extension was purely
# for explorability originally — #2663 additionally reuses it as the
# read-side recovery channel (:func:`mime_type_for_ext`) so present's
# stage-3 default viewer can recover a ``data_ref``'s declared content type
# from the stored ref's extension, without any new sidecar field.
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/xml": ".xml",
}

# The reverse of ``_MIME_TO_EXT`` (built once, module load) — ``.txt``/``.json``
# collide with no other extension so the mapping is unambiguous either direction.
_EXT_TO_MIME: dict[str, str] = {ext: mime for mime, ext in _MIME_TO_EXT.items()}


def _ext_for_mime(mime: str) -> str:
    """Return the file extension (with leading dot) for ``mime``.

    Strips any ``; charset=...`` suffix before lookup. Returns ``""`` for
    unknown types — caller still writes the file, just without a hint.
    """
    base = mime.split(";", 1)[0].strip().lower() if mime else ""
    return _MIME_TO_EXT.get(base, "")


def mime_type_for_ext(path: str) -> "str | None":
    """Return the declared MIME type for a stored ref's file extension, or ``None``
    when the extension is unknown/absent (#2663 — the read-side counterpart of
    :func:`_ext_for_mime`, the write-side lookup ``save_tool_result``'s ``mime_type``
    already drives). This is the ONLY channel present's stage-3 default viewer uses to
    recover a ``data_ref``'s content type — no separate sidecar metadata file is written;
    the extension IS the sidecar, already persisted by the existing store write path."""
    suffix = Path(path).suffix.lower()
    return _EXT_TO_MIME.get(suffix)


def _safe_token(value: str) -> str:
    """Sanitise a value for embedding in a filename.

    Replaces path-separators, spaces, and other shell-unfriendly chars
    with ``_``. Keeps the result reasonable on common filesystems
    (Linux / macOS / Windows).
    """
    if not value:
        return ""
    out = []
    for ch in value:
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def _timestamp() -> str:
    """``YYYYMMDDTHHMMSS`` UTC timestamp used as the filename prefix."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


@dataclass
class MediaStoreConfig:
    """Storage location configuration for :class:`MediaStore`.

    Paths are interpreted relative to ``project_root`` (= the chat
    session's CWD-rooted workspace). Defaults match the user-browsable
    convention chosen in issue #383 / #385:
        ``.reyn/media``        for image binary
        ``.reyn/tool-results`` for text-y tool result dumps

    Phase 1 (= "(a) Persistent until user delete", #385 β sub-task 5):
    no cleanup-policy fields here today — the storage is intentionally
    unbounded, audit is via on-disk inspection. Phase 2 (= TTL / max-N /
    session-end / mixed) extends this dataclass when measurement
    surfaces a real disk-pressure or stale-handle problem. The
    extension surface is documented to keep the future addition out of
    the public-API surprise zone, but no fields are reserved today
    (= YAGNI; field shape is a Phase 2 design decision).

    #5364 §1.6 is that Phase 2 trigger, but ONLY for
    ``history_content_dir`` (measurement surfaced real GB-class growth
    there specifically — see ``history_content_max_bytes`` below); the
    older ``media_dir``/``tool_results_dir`` fields are unaffected and
    still carry no cleanup policy of their own.
    """
    media_dir: str = ".reyn/media"
    tool_results_dir: str = ".reyn/tool-results"
    # #5364 §1.1: the CURRENT tool-result write location (session-scoped —
    # ``<session_id>`` is appended by :class:`MediaStore` itself, never
    # part of this literal). A separate field from ``tool_results_dir``
    # (lead-coder review, PR #5369: "旧 store は同じ file の 2 行上で
    # self._config.tool_results_dir を通しています ... 新しい store は別
    # の field に") — ``tool_results_dir`` names the FROZEN, read-only
    # legacy location; conflating the two would let one config change
    # silently redirect BOTH the live write path and the historical read
    # boundary at once, which is never what an operator overriding one
    # of them actually wants.
    history_content_dir: str = ".reyn/memory/history-content"
    # #5364 §1.6: a per-project BACKSTOP against runaway growth of
    # history_content_dir — NOT a routine management knob (owner: "not
    # something to run day-to-day"). A separate field from
    # tool_results_dir/history_content_dir (same reasoning as
    # history_content_dir's own separation above — one knob must not
    # double as two different subjects' cap).
    #
    # This alone does NOT bound the total size any operator's disk sees:
    # what actually grows without bound is not one session's own content
    # (this field's subject) but the NUMBER of sessions a project
    # accumulates — that cross-session subject is #5366 (owner-ruled
    # separation), deliberately NOT this field's job.
    #
    # #5366 gave that separate subject its own number:
    # ``reyn.config.infra.StorageConfig.max_bytes`` (``storage:`` in
    # reyn.yaml) — a DIFFERENT number bounding the WHOLE project's
    # history-content tree across every session, never reused as this
    # field's own name (architect's ruling: two same-named caps would
    # make it unreadable which one is actually in effect for a given
    # eviction). This field remains the per-store fail-safe backstop
    # #5388's own per-session eviction uses; it is unaffected by whether
    # ``StorageConfig.max_bytes`` is set.
    #
    # Default derivation (owner ruling, measured 2026-08-28, 8 projects
    # on this machine): the ONE project with any ``.reyn/tool-results/``
    # content at all held 18 files / 196 KB; the largest ``.reyn/`` tree
    # measured across all 8 was 132 MB. 2 GB is >15x that observed
    # maximum — the cap is intended to never fire under any measured
    # real usage; it exists only to bound a genuine runaway.
    #
    # #5387 landed the discriminator this used to lack: ``chain_id`` now
    # threads through the write-time cap path too (previously only the
    # REACTIVE-spill path, ``router_history_buffer.py``, carried one), so
    # ``MediaStore.is_open_turn_file`` can tell "this session's newest
    # file" apart from "a turn currently in flight" WITHOUT approximating
    # via mtime (mtime is NOT safe for this — lead-coder ruling, #5364:
    # "the newest file is not a turn boundary"; architect, same ruling:
    # mtime answers "when was this written", not "whose turn is this" —
    # two concurrent sessions writing at once interleave mtime order
    # across BOTH turns). See ``protect_open_turn_from_gc`` below for the
    # resulting default and its scope.
    #
    # A lower cap can now safely fire without destroying real, in-flight
    # turn content — see ``protect_open_turn_from_gc``'s own docstring
    # for the one thing it still does NOT cover (another session's own
    # open turn — #5366's subject, not this field's).
    history_content_max_bytes: int = 2 * 1024 * 1024 * 1024
    # #5387 "L" (owner: "open-turn は GC 対象から外せるようにしたいね。規定は
    # そうして、opt-in で対象にもするようにしたい" — verbatim): when True
    # (the default), ``_evict_history_content_over_cap`` never deletes a
    # file whose recorded ``chain_id`` matches the chain that triggered
    # THIS eviction pass (``MediaStore.is_open_turn_file`` — GC is
    # writer-triggered, so that chain IS "the turn currently in flight").
    # Set False to opt IN to eviction being allowed to reach open-turn
    # content too (the owner's own "opt-in で対象に" — e.g. an operator who
    # has independently decided in-flight content is not worth protecting
    # at their site).
    #
    # Scope (architect design B, #5387 — stated explicitly so this is not
    # mistaken for "we decided not to bother"): this protects ONLY the
    # write that triggered THIS session's own GC pass. Another session's
    # own open turn is out of reach entirely — the eviction scan only
    # ever enumerates THIS session's own directory (see
    # ``_evict_history_content_over_cap``'s own docstring) — and stays
    # unprotected until a CROSS-session GC (#5366) is built; #5366 is
    # where that would first need to be handled, not here.
    protect_open_turn_from_gc: bool = True


@dataclass(frozen=True)
class MediaStorageStats:
    """#4478 Phase 1: policy-independent on-disk footprint snapshot for
    :meth:`MediaStore.storage_stats`. Measurement only — no field here
    implies or drives any eviction; see that method's docstring."""
    media_file_count: int
    media_bytes: int
    tool_result_file_count: int
    tool_result_bytes: int


def _dir_stats(directory: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for the flat, single-level layout both
    storage dirs use (see the module docstring's filename convention —
    neither dir nests subdirectories). Missing directory → ``(0, 0)``,
    same as "nothing written yet" rather than an error; a file that
    disappears mid-scan (a concurrent delete, raising
    ``FileNotFoundError``) is skipped rather than propagating, since this
    is a best-effort snapshot, not a transactional read.

    #4671 census: any OTHER ``OSError`` (e.g. a permission error) is
    deliberately NOT swallowed here — a prior revision caught bare
    ``OSError``, which would silently under-count a file this process
    genuinely could not measure, with no disclosure that anything was
    skipped (same defect class as ``history_tail_reader.history_file_stats``,
    #4671's own reported crash — there the same overbroad-except shape
    manifested as a raised exception instead of a silent undercount,
    because the failing check and the failing use weren't in the same
    try block there)."""
    if not directory.is_dir():
        return 0, 0
    count = 0
    total = 0
    for entry in directory.iterdir():
        try:
            if entry.is_file():
                count += 1
                total += entry.stat().st_size
        except FileNotFoundError:
            continue
    return count, total


def _dir_stats_recursive(directory: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for a NESTED layout — #5364's
    ``history-content/<session_id>/`` tree, one flat level PER session
    rather than one flat level overall. Same best-effort/error-disclosure
    policy as :func:`_dir_stats` (see its own docstring); ``rglob`` walks
    every session subdirectory in one pass."""
    if not directory.is_dir():
        return 0, 0
    count = 0
    total = 0
    for entry in directory.rglob("*"):
        try:
            if entry.is_file():
                count += 1
                total += entry.stat().st_size
        except FileNotFoundError:
            continue
    return count, total


def _eviction_order(directory: Path) -> list[Path]:
    """#5364 §1.6 "C": among files that ARE eligible for deletion, which
    one goes first — oldest ``mtime`` first, the only rule today.

    This is an ORDER over an already-eligible set, not an eligibility
    filter — the two are different questions with different failure
    modes (lead-coder review, PR #5388: "may this be deleted" is a
    filter, "which one first" is a sort, and folding a future filter
    into THIS function's sort key would let it silently become "deleted
    last" instead of "never deleted"). #5387's turn-boundary exclusion
    lands as exactly that SEPARATE filter, applied to this function's
    OUTPUT by :meth:`MediaStore._evict_history_content_over_cap` (never
    folded into this function's own sort) — not this function's job.
    mtime cannot stand in for that filter either way: see
    ``history_content_max_bytes``'s own docstring for why (two
    concurrent sessions' writes interleave in mtime order — "oldest" is
    not "whose turn is still open"). Best-effort on a ``stat()`` race (a
    file removed between listing and stat) — same disclosed-skip policy
    as :func:`_dir_stats`."""
    entries: list[tuple[float, Path]] = []
    for entry in directory.rglob("*"):
        try:
            if entry.is_file():
                entries.append((entry.stat().st_mtime, entry))
        except FileNotFoundError:
            continue
    entries.sort(key=lambda pair: pair[0])
    return [path for _, path in entries]


# #385 β core impl sub-task 1: cross-host capable resource URI scheme.
# A path-ref minted with ``agent_name`` set carries this scheme so a
# downstream consumer (= another agent / a different host) can dispatch
# back to the producing agent's MediaStore. Sub-task 3 will wire the
# cross-host RPC; this sub-task lands the schema + same-host resolution.
_RESOURCE_URI_SCHEME = "reyn-tool-result://"


def parse_resource_uri(uri: str) -> tuple[str, str] | None:
    """Parse a ``reyn-tool-result://<agent>/<artifact>`` URI.

    Returns ``(agent, artifact)`` on a successful parse, or ``None`` when
    the input doesn't match the expected scheme / shape. The artifact
    portion may itself contain ``/`` (= not consumed by the split) so
    nested-path artifacts remain addressable; ``agent`` is always the
    single segment between the scheme and the first ``/``.
    """
    if not isinstance(uri, str) or not uri.startswith(_RESOURCE_URI_SCHEME):
        return None
    rest = uri[len(_RESOURCE_URI_SCHEME):]
    if "/" not in rest:
        return None
    agent, artifact = rest.split("/", 1)
    if not agent or not artifact:
        return None
    return agent, artifact


def history_content_root_for(
    project_root: Path, config: "MediaStoreConfig | None" = None,
) -> Path:
    """#5366: the project-wide root EVERY session's own history-content
    directory nests under (``<root>/<agent>/<session_id>/``) — for a
    caller that needs the WHOLE tree (the project-wide GC's own
    candidate scan), not one session's own subdirectory
    (:func:`history_content_dir_for` is that narrower sibling). Same
    "one source of truth for the path shape" reasoning as that
    function's own docstring — never creates the directory."""
    cfg = config or MediaStoreConfig()
    return (project_root / cfg.history_content_dir).resolve()


def cross_session_eviction_candidates(
    root: Path, *, pin: "list[str] | None" = None,
) -> list[Path]:
    """#5366 §3 (architect design, revised — issuecomment-5451564768):
    the project-wide GC's own candidate set. Every file
    :func:`_eviction_order` finds under *root* (oldest-first, across
    EVERY session — #5383's own ``<agent>/<sid>/`` nesting means
    attribution is readable straight from each path's first two
    segments), minus any file whose ``<agent>`` segment names a pinned
    agent.

    Deliberately NO liveness filter (architect's own reversal after
    e2e-coder's #5366 measurement found ``process_registry``'s own
    marker carries no agent/session identity at all — {pid, ppid, cwd,
    subcommand, started_at}, registered before any agent is even
    resolved): #5388's per-session cap already evicts a LIVE session's
    own old files on every write, protecting only that write's own open
    turn (#5387's ``is_open_turn_file``) — a cross-session liveness
    protection here would guard cross-session GC MORE than per-session
    GC already guards itself, for the same resource, same content, same
    operator, with no reason found for the asymmetry. Only pin
    (operator-declared) and the write-driven open-turn protection
    (owned by #5388/#5387, not this function) are real filters.

    ⚠️ Disclosed, not solved (architect, same ruling): a project-wide
    sweep running while a DIFFERENT session has a turn genuinely
    in-flight can evict that turn's own content — there is no marker
    today that could tell this scan "that file is mid-turn for a
    session that isn't mine" (the #5387 discriminator is write-driven,
    scoped to the session doing the writing). A read that later hits
    the evicted path resolves as ``lost`` (#5364-B's own vocabulary) —
    silence here would read as "cross-session GC is safe", which it is
    not for that one window. Re-visit trigger (owner's own discipline:
    do not build for something not yet observed): a real observation of
    a cross-session sweep immediately followed by another session's own
    in-flight ref reading back ``lost``."""
    ordered = _eviction_order(root)
    if not pin:
        return ordered
    pinned = {_safe_token(name) for name in pin}
    candidates = []
    for path in ordered:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if parts and parts[0] in pinned:
            continue
        candidates.append(path)
    return candidates


def history_content_dir_for(
    project_root: Path, agent_name: str, session_id: str,
    config: "MediaStoreConfig | None" = None,
) -> Path:
    """#5364 §1.6 "Q": the SAME path :meth:`MediaStore._history_content_dir`
    computes for a LIVE store, exposed as a standalone function so a
    caller that only needs to know WHERE a session's content lives (e.g.
    the registry's own session-vanish purge) doesn't have to construct a
    full ``MediaStore`` instance — one source of truth for the path
    shape, not a second hand-derived copy that could drift from the
    real one. Never creates the directory (a vanished session may never
    have written anything at all — the caller is expected to check
    ``is_dir()`` before acting, same as ``registry._purge_session_dir``
    already does for the state dir)."""
    cfg = config or MediaStoreConfig()
    root = (project_root / cfg.history_content_dir).resolve()
    return root / _safe_token(agent_name) / _safe_token(session_id)


class MediaStoreWriteUnavailable(Exception):
    """#5364 §1.5: raised by :meth:`MediaStore.save_tool_result` instead of
    returning a ref block, when this store's writes are known NOT to land —
    either this exact call's own write failed synchronously (the
    no-running-loop / sync-fallback path — an operator script, a CLI
    command, a sync test), or an EARLIER write already exhausted
    :class:`~reyn.core.events.durability_worker.DurabilityWorker`'s §4
    retry bound and latched ``durability_failed`` (a chat-turn call,
    off-loop — that failure was necessarily discovered async, after this
    call's own synchronous return already happened once before, so this
    exception only ever protects the NEXT attempt).

    The caller (``tool_result_cap.cap_tool_result_content``) catches this
    and keeps the content INLINE instead — #5364 §1.5's own requirement:
    "a permanently-failed write's turn keeps content inline, never emits a
    ref naming a file that doesn't exist." Never raised for a transient
    (retryable) failure — those stay entirely inside the worker's own
    bounded-backoff retry, invisible here."""


class MediaStore:
    """Path-ref'd file storage for multimodal media + tool result text.

    Each ``save_*`` call writes a file under the appropriate directory
    and returns a **path-ref block** suitable for placement in a
    ``ChatMessage.content`` list (= part of the OpenAI/Anthropic wire
    shape mirror; see issue #383). The corresponding ``read_*`` methods
    do the inverse lookup with workspace-boundary validation.

    Path-ref shape (#385 β core impl sub-task 1+3b, 2026-05-22 frozen
    contract): when ``agent_name`` is supplied at construction, save_*
    returns the extended shape that carries cross-host routing fields::

        {
          "type": "tool_result_ref" | "image",
          "path": "<project-relative>",       # same-host fast-path
          "resource_uri": "reyn-tool-result://<agent_name>/<filename>",
          "source_agent": "<agent_name>",     # durable identity for dispatch
          "source_chain_id": "<chain_id>",    # audit annotation only (optional)
          # When ``base_url`` is ALSO set (= sub-task 3b cross-host
          # transport surface):
          "url": "<base_url>/agents/<agent_name>/tool-results/<filename>",
          "mime_type": "...",
          "content_hash": "sha256:...",
        }

    When ``agent_name`` is omitted (= legacy call sites, test stubs),
    save_* returns the pre-β shape (= no resource_uri / source_agent /
    source_chain_id / url, just ``path``). Consumers must treat the
    cross-host fields as optional: when ``url`` is present a consumer
    can HTTP GET it cross-host; when only ``resource_uri`` is present
    the consumer knows the identity but has no transport (= producer
    deployed without ``reyn web`` or without ``multimodal.base_url``
    set); when neither is present, only the same-host ``path`` works.

    Cross-host RPC routing (sub-task 3 of the β core impl) is NOT
    implemented in this sub-task — ``read_tool_result_by_uri`` raises
    ``ValueError`` when the URI's source_agent doesn't match this
    store's identity, with a clear "cross-host not yet supported"
    message so the read_tool_result handler can surface a stub error.
    """

    def __init__(
        self,
        config: MediaStoreConfig | None = None,
        *,
        project_root: Path,
        agent_name: str | None = None,
        base_url: str | None = None,
        session_id: str,
        worker: "DurabilityWorker | None" = None,
    ) -> None:
        self._config = config or MediaStoreConfig()
        self._project_root = project_root.resolve()
        # #5364 §1.4 (owner: "UIを止めさせたくない" — a synchronous tool-result
        # write can stall the event loop, same class of problem #1765 fixed
        # for the WAL; see :meth:`save_tool_result`'s own docstring). Same
        # lazy-default shape as EventStore's own worker (event_store.py) —
        # this store's writes have no ordering dependency on the WAL's, so a
        # DEDICATED worker (not the session's shared one) keeps the two
        # substrates decoupled, matching EventStore's own precedent rather
        # than StateLog's cross-substrate ``submit_durable`` sharing (that
        # sharing exists because a snapshot's ``applied_seq`` must never
        # precede the WAL entry it names — no such dependency here).
        if worker is None:
            from reyn.core.events.durability_worker import DurabilityWorker
            worker = DurabilityWorker()
        self._worker = worker
        self._media_dir = (
            self._project_root / self._config.media_dir
        ).resolve()
        # #4381/#5364: `tool_results_dir` (`.reyn/tool-results/`) is the
        # PRE-#5364 write location — kept, untouched, READ-ONLY going
        # forward (see :meth:`save_tool_result`'s own docstring for why
        # writes moved). No migration script: an old path-ref still
        # resolves here exactly as it always did.
        self._tool_results_dir = (
            self._project_root / self._config.tool_results_dir
        ).resolve()
        # #5364 §1.1: new tool-result writes go under a PERSIST-tier,
        # session-scoped, NESTED directory — `memory/` directly, never a
        # flat `.md`, because 4 non-recursive `glob("*.md")` scanners walk
        # `memory/`'s own direct children and would each read this store's
        # entire multi-GB footprint if it lived there unnested (#5364,
        # tui-coder's 4-scanner census). `_history_content_root` (the
        # PARENT of every session's own subdirectory) is also the read
        # boundary `read_tool_result` validates new-format paths against.
        self._history_content_root = (
            self._project_root / self._config.history_content_dir
        ).resolve()
        # #5364 (lead-coder review, PR #5369): NO default here — a
        # forgotten session_id must never silently resolve to some OTHER
        # real session's directory. Required kwarg (option (a) of the
        # two the review offered): a construction site with nothing real
        # to pass (4 of production's 5 sites never write a tool result at
        # all) supplies a self-documenting placeholder (e.g.
        # ``"<read-only>"``) rather than omitting it — omitting it is a
        # TypeError at construction, not a silent write into another
        # session's directory.
        self._session_id = session_id
        self._agent_name = agent_name or None
        # #385 β sub-task 3b: when set, save_* augments path-refs with a
        # ``url`` field pointing at this Reyn instance's resources router
        # (= ``<base_url>/agents/<agent>/tool-results/<artifact>``) so
        # cross-host consumers can fetch via standard HTTP GET. Unset →
        # no ``url`` minted, same-host fast-path only.
        self._base_url = (base_url or "").rstrip("/") or None
        # #4381: every path this store has itself written via
        # ``save_tool_result`` (= a "tool result spill" — owner-ratified
        # term, 2026-08-12: "入らないから出す。不可避・設定で止めない",
        # distinct from "offload" = "入るけれども節約のために出す。最適化・
        # 既定 false"). ``op_runtime/file.py``'s read handler queries this
        # via :meth:`is_tool_result_spill` to detect and break the loop a
        # *bare* re-read of a spilled file can otherwise cause: reading the
        # spill file's own content can be too big for ``file.py``'s
        # window-derived cap AGAIN, and since that cap is INDEPENDENT of
        # the router's separate token-derived spill trigger
        # (``services/tool_result_cap.py``), a naive truncate-and-return
        # can come back oversized under the ROUTER's cap too and get
        # spilled A SECOND time — a real, unbounded chain, not a
        # hypothetical.
        #
        # PERSISTED (lead-coder review, #4381): a REFERENCE to a spill
        # path survives across a restart — it can sit in ``history.jsonl``
        # (the LLM's own past ``read_file(path=<spill>)`` call, or a tool-
        # result-ref block from a still-open turn) long after the process
        # that wrote the spill has exited. An in-memory-only set is EMPTY
        # in the next process, so the guard would silently not fire for a
        # path that genuinely is a spill — the loop this exists to close
        # reopens across the restart boundary, not just within one
        # process. Loaded once at construction from
        # :data:`_SPILL_MANIFEST_FILENAME` under ``tool_results_dir``,
        # appended to (one line per write) by :meth:`save_tool_result`.
        # #4584: this manifest self-PRUNES (an existing entry whose target
        # vanished is dropped — see :meth:`_load_spill_manifest`) but has NO
        # REBUILD story: if the manifest FILE ITSELF were deleted, nothing
        # recreates it from another source. An earlier version of this
        # comment conflated the two ("rebuilt/pruned like every other cache
        # entry" — false: no rebuild mechanism was ever implemented,
        # measured directly; see :data:`_SPILL_MANIFEST_FILENAME`'s own
        # module-level docstring for the full correction). It otherwise
        # survives for the project's lifetime, same as :mod:`data.workspace.
        # artifact_ref`'s sibling table.
        self._tool_result_spill_paths: "set[Path]" = self._load_spill_manifest()
        # #5387: the write-time cap path's ONE consumer of ``chain_id`` —
        # NOT persisted (lead-coder ruling: GC is writer-triggered, so the
        # chain that fired THIS eviction pass IS "the turn currently in
        # flight"; a file from a PAST process's chain is, by definition,
        # never the current one — nothing here needs to survive a
        # restart). Populated only when a real (non-empty) ``chain_id``
        # reaches :meth:`save_tool_result`; never read for a path whose
        # entry is absent — see :meth:`is_open_turn_file`.
        self._chain_by_path: "dict[Path, str]" = {}

    def _spill_manifest_path(self) -> Path:
        # #4584: PERSIST tier — `.reyn/memory/`, not `.reyn/cache/` (see
        # :data:`_SPILL_MANIFEST_FILENAME`'s own module docstring).
        return self._project_root / ".reyn" / "memory" / _SPILL_MANIFEST_FILENAME

    def _load_spill_manifest(self) -> "set[Path]":
        manifest = self._spill_manifest_path()
        if not manifest.exists():
            return set()
        paths: "set[Path]" = set()
        stale = False
        try:
            for line in manifest.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    p = Path(entry["path"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # one malformed line never invalidates the rest
                # #4478: an entry whose target no longer exists on disk (the
                # artifact was manually deleted, or GC'd by a future Phase 2
                # policy) protects nothing — is_tool_result_spill only needs
                # to answer "is THIS path a spill I wrote", and a re-read of
                # a now-missing path already fails on its own, normally,
                # with no re-spill loop to guard against. Dropping it here
                # is what bounds this manifest's otherwise-unbounded growth
                # (lead-coder's #4478 dispatch, concern ②: it is read in
                # full on every MediaStore construction).
                if p.exists():
                    paths.add(p)
                else:
                    stale = True
        except OSError:
            return set()  # best-effort — an unreadable manifest degrades to empty, not a crash
        if stale:
            self._persist_spill_manifest(paths)
        return paths

    def _persist_spill_manifest(self, paths: "set[Path]") -> None:
        """#4478: rewrite the MANIFEST ONLY — the ledger of which paths
        this store has spilled, under ``.reyn/memory/`` (#4584: moved from
        ``.reyn/cache/``). Never touches an artifact under
        ``tool_results_dir`` itself; an entry only reaches this rewrite
        because :meth:`_load_spill_manifest` already found its target file
        gone from disk (deleted by something else, out of scope for this
        store). This prune deletes zero bytes of anyone's actual content —
        it only stops re-reading a manifest line that stopped meaning
        anything the moment its file disappeared.

        #4584: this SELF-PRUNE (an existing, real entry dropped once its
        target vanishes) is NOT the same claim as "this manifest can be
        REBUILT" (recreated wholesale after the manifest FILE ITSELF is
        deleted) — an earlier comment on this module conflated the two
        ("rebuilt/pruned like every other cache entry"). Only pruning is
        real; there is no reconstruction-from-nothing path, which is
        exactly why this manifest cannot live under a tier documented
        "safe to delete, rebuilt after restore" (see
        :data:`_SPILL_MANIFEST_FILENAME`'s own module docstring).

        Best-effort, same as the append path in :meth:`save_tool_result` —
        a write failure here (e.g. a read-only ``.reyn/memory/``) must
        never fail construction; it only means this process's prune
        didn't stick and the next one retries."""
        manifest = self._spill_manifest_path()
        try:
            manifest.write_text(
                "".join(json.dumps({"path": str(p)}) + "\n" for p in sorted(paths)),
                encoding="utf-8",
            )
        except OSError:
            pass

    def is_tool_result_spill(self, path: "str | Path") -> bool:
        """#4381: whether *path* is a file THIS store itself wrote via
        :meth:`save_tool_result` — i.e. a tool-result spill artifact, not
        an arbitrary file that merely happens to live under
        ``tool_results_dir`` (a path-naming/location heuristic would also
        catch an unrelated file an operator happened to place in the same
        directory — the reason this checks WHAT WAS ACTUALLY WRITTEN,
        never where a path merely sits). Persisted (see the manifest note
        in ``__init__``) — recognizes a spill written by an EARLIER
        process too, not just this one.

        Resolves *path* the same way ``save_tool_result`` resolved the
        path it recorded (against ``project_root`` when relative), so a
        caller may pass either the project-relative path a tool result
        block carries or an absolute one."""
        p = Path(path)
        if not p.is_absolute():
            p = (self._project_root / p).resolve()
        else:
            p = p.resolve()
        return p in self._tool_result_spill_paths

    # ── Image storage (= .reyn/media/) ────────────────────────────────

    def save_image(
        self,
        data: bytes,
        *,
        mime_type: str,
        chain_id: str = "",
        tool: str = "tool",
        seq: int = 1,
    ) -> dict:
        """Write ``data`` to a new file under ``media_dir`` and return a
        path-ref block (= ``{"type": "image", "path": ..., "mime_type":
        ..., "content_hash": ...}``).

        ``chain_id`` (= short prefix), ``tool``, and ``seq`` are encoded
        into the filename for explorability. ``content_hash`` is the
        SHA-256 of ``data`` (= verifies the path-ref hasn't drifted
        from the original content; used by the history builder when
        materialising back to a data URL).
        """
        self._media_dir.mkdir(parents=True, exist_ok=True)
        chain_short = _safe_token(chain_id)[:6] if chain_id else ""
        tool_token = _safe_token(tool) or "tool"
        ext = _ext_for_mime(mime_type)
        filename = f"{_timestamp()}-{chain_short}-{tool_token}-{seq}{ext}"
        path = self._media_dir / filename
        path.write_bytes(data)
        block: dict = {
            "type": "image",
            "path": str(path.relative_to(self._project_root)),
            "mime_type": mime_type,
            "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
        self._attach_cross_host_fields(block, filename=filename, chain_id=chain_id)
        return block

    def read_image(self, path_str: str) -> tuple[bytes, bool]:
        """Read image binary by project-relative path.

        Validates the resolved path lives inside ``media_dir`` (=
        defends against path-traversal injection from migrated /
        adversarial ChatMessage content). Returns ``(data, found)``;
        ``found=False`` if the file does not exist OR was deleted by
        the user since the path-ref was minted.
        """
        full = (self._project_root / path_str).resolve()
        try:
            full.relative_to(self._media_dir)
        except ValueError as exc:
            raise PermissionError(
                f"path {path_str!r} is outside media_dir "
                f"{self._media_dir} — refusing to read"
            ) from exc
        if not full.exists():
            return b"", False
        return full.read_bytes(), True

    # ── Tool result storage (= .reyn/memory/history-content/<session_id>/) ──

    def _history_content_dir(self) -> Path:
        """This session's own subdirectory under ``_history_content_root``
        (#5364 §1.1 "★M ── ディレクトリはsession単位"), NESTED one level
        further under this store's own agent (#5364 issue-body fixup,
        architect ruling): ``session_id`` alone is not agent-unique — the
        default session id (``registry._DEFAULT_SID``, ``"main"``) is the
        SAME literal for every agent, so keying on ``session_id`` alone
        let every agent's main session share one directory (measured: a
        real key-space defect in #5369, merged before this fix). This
        store already declares its own agent identity elsewhere (see
        :meth:`read_tool_result_by_uri`'s cross-host agent-match check,
        ``"This store's identity is {self._agent_name!r}"``) — nesting
        under it here makes THIS path agree with that same identity,
        and makes the key-space match ``registry._session_state_dir``'s
        own ``(name, sid)`` shape exactly (the precondition #5364 §1.6
        "Q" needs to wire session-vanish purge safely).

        ``_history_content_root`` itself is UNCHANGED (not nested under
        agent) — an already-minted flat ref must keep resolving inside
        the same boundary (no migration script, same reasoning as
        #5364 §1.1's own root-boundary decision); only NEW writes land
        one level deeper.

        Raises :class:`ValueError` if this store has no agent identity
        (``agent_name`` was never given at construction) — the same
        "raise at the one point that actually needs it" shape
        ``session_id`` already uses (required there; ``agent_name``
        stays optional at construction — 4 of 5 production sites are
        read-only and legitimately have none — so the check lives here,
        the one method that actually needs a real value). Falling back
        to a shared directory would reproduce the exact defect this
        fixes.

        Created lazily by :func:`offload_value`'s own write path, same
        as every other MediaStore directory — never pre-created here.
        """
        if not self._agent_name:
            raise ValueError(
                "MediaStore.save_tool_result requires a real agent_name — "
                "the write-time directory is agent-scoped (a missing "
                "agent_name must never silently fall into a directory "
                "shared with other agents' sessions, #5364)"
            )
        # Delegates to the standalone function below — one source of
        # truth for the path shape (see its own docstring: a caller with
        # no live store, e.g. the registry's session-vanish purge, needs
        # the SAME computation without constructing a MediaStore).
        return history_content_dir_for(
            self._project_root, self._agent_name, self._session_id, self._config,
        )

    def save_tool_result(
        self,
        content: "str | dict",
        *,
        mime_type: str = "text/plain",
        chain_id: str = "",
        tool: str = "tool",
        seq: int = 1,
        payload_field: str | None = None,
    ) -> dict:
        """Write a tool result text dump under this session's
        ``history-content`` directory (#5364 §1.1/§1.4 — the SAME write
        seam every reactive-spill / write-time-cap caller already uses;
        only the destination directory moved) and return a path-ref block
        (= ``{"type": "tool_result_ref", "path": ..., "mime_type": ...,
        "content_hash": ...}``).

        The LOCAL store + hash is delegated to :func:`offload_value` from
        ``services/offload/store.py`` (Phase 2 of the offload-dedup effort,
        FP-0008 C5 #223).  The return block shape is IDENTICAL to the
        pre-migration contract — callers are unaffected.

        #5364: this is deliberately the ONE write call site
        (``is_tool_result_spill`` checks WHAT WAS ACTUALLY WRITTEN via the
        in-memory + persisted manifest below, keyed off whatever directory
        THIS method resolves — a second write call site would silently
        stop populating that manifest for its own files). The pre-#5364
        ``.reyn/tool-results/`` directory is never written here again —
        see :meth:`read_tool_result` for why old path-refs still resolve.

        #2394-followup clean-payload: when ``content`` is the op-result dict and
        ``payload_field`` names its sole-oversized field, ``offload_value`` stores
        THAT field's value CLEAN (raw text with real newlines) instead of the whole
        dict — the chat path uses this so an offloaded MCP/web/exec result is clean
        content, not a ``{"status":...,"data":{...}}`` single-line envelope. When
        ``content`` is a string (the default), behaviour is byte-identical to before.

        Preview-bound note: ``preview_strategy=None`` is passed so the
        common service performs no preview bounding.  The preview is built
        externally by the caller (e.g. ``web.py`` ``_generate_web_fetch_preview``).
        See the ★ three-party bound contract in
        ``services/offload/store.py`` module docstring.

        #5364 §1.5 (owner: "a permanently-failed write's turn keeps
        content inline, never emits a ref naming a file that doesn't
        exist"): raises :class:`MediaStoreWriteUnavailable` instead of
        returning a block when this store's writes are known not to
        land — see that exception's own docstring for the two ways this
        can be discovered. ``cap_tool_result_content`` (this method's
        one real caller) catches it and keeps the content inline.
        """
        if self.durability_failed:
            raise MediaStoreWriteUnavailable(
                "MediaStore's durable-write worker has a PERSISTENT "
                "failure latched (DurabilityWorker.durability_failed) — "
                "refusing to mint a ref for a file that will not exist"
            )
        chain_short = _safe_token(chain_id)[:6] if chain_id else ""
        tool_token = _safe_token(tool) or "tool"
        ext = _ext_for_mime(mime_type)
        filename = f"{_timestamp()}-{chain_short}-{tool_token}-{seq}{ext}"

        try:
            result = offload_value(
                content,
                store_dir=self._history_content_dir(),
                preview_strategy=None,
                filename=filename,
                payload_field=payload_field,
                # #5364 §1.4: the actual disk write moves off the event loop
                # (fire-and-forget, serial FIFO) — see :meth:`flush`'s own
                # docstring for the barrier this relies on to make the write
                # observable before anything could try to read it back.
                submit_nowait=self._submit_write_or_inline,
            )
        except OSError as exc:
            # #5364 §1.5: only reachable via the SYNC-FALLBACK branch of
            # _submit_write_or_inline (no running loop — the write ran
            # inline, synchronously, so ITS OWN failure is known here and
            # now, unlike the async/worker path's failures, which are
            # necessarily discovered later — see MediaStoreWriteUnavailable's
            # own docstring). No retry here: the async path already owns
            # retry (DurabilityWorker §4); this path only ever runs when no
            # loop is present to defer to it in the first place.
            raise MediaStoreWriteUnavailable(
                f"the synchronous (no-running-loop) write failed: {exc}"
            ) from exc

        # Convert absolute path_ref to project-relative path for the block.
        abs_path = Path(result.path_ref)
        rel_path = str(abs_path.relative_to(self._project_root))
        # #4381: record so a later ``read_file`` on this exact path — in
        # THIS process or a LATER one (a reference to it can sit in
        # ``history.jsonl`` past a restart) — can detect it's re-reading a
        # spill artifact (see :meth:`is_tool_result_spill`'s own
        # docstring for why). The in-memory set update is immediate
        # (in-memory, no I/O, no ordering hazard — protects a re-read
        # within THIS same turn even before the content write is durable).
        # The manifest append is independent, best-effort: a manifest-
        # write failure (e.g. a read-only ``tool_results_dir``, OR — #5364
        # §1.4 — a process crash between this line ENQUEUING the deferred
        # append and its job actually running, since it is fire-and-forget
        # off-loop just like the content write above) must never fail the
        # spill write itself, which is the load-bearing operation here —
        # it only means a FUTURE process's guard won't recognize this one
        # path, not that this write failed.
        #
        # #5364 §1.4: submitted via the SAME deferral path as the content
        # write above, ALWAYS after it (same worker, FIFO-serial — see
        # ``DurabilityWorker``'s own "enqueue order = durability order"
        # guarantee) — never inline-synchronous here. `_load_spill_
        # manifest`'s self-prune (see its own docstring) drops any entry
        # whose target file doesn't exist ON DISK YET; writing this line
        # before the content write actually landed would let a FRESH
        # MediaStore instance's self-prune permanently discard a
        # perfectly-good, merely-still-queued entry — ordering, not just
        # eventual completion, is what this file's manifest contract needs.
        resolved_path = abs_path.resolve()
        self._tool_result_spill_paths.add(resolved_path)
        # #5387: record this write's chain_id (in-memory, per-process —
        # see the field's own docstring) so a LATER eviction pass this
        # same process runs can tell "this file belongs to the turn that
        # just triggered GC" apart from everything else. A caller that
        # passes no chain_id (still the default at some call sites) never
        # gets an entry — such a file is never treated as open-turn.
        if chain_id:
            self._chain_by_path[resolved_path] = chain_id

        async def _write_manifest_line(_resolved_path: Path = resolved_path) -> None:
            try:
                manifest_path = self._spill_manifest_path()
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps({"path": str(_resolved_path)}) + "\n"

                def _append() -> None:
                    with manifest_path.open("a", encoding="utf-8") as f:
                        f.write(line)
                await asyncio.to_thread(_append)
            except OSError:
                pass
        self._submit_write_or_inline(_write_manifest_line)
        block: dict = {
            "type": "tool_result_ref",
            "path": rel_path,
            "mime_type": mime_type,
            "content_hash": result.content_hash,
        }
        self._attach_cross_host_fields(block, filename=filename, chain_id=chain_id)
        # #5364 §1.6 "C": bound THIS session's own history-content
        # footprint after every write — a per-write check (not a
        # separate sweep) keeps the cap self-enforcing without a second
        # scheduled mechanism. Runs against whatever is ALREADY on disk;
        # the write just submitted above is off-loop and may not have
        # landed yet, so this can lag one write behind — acceptable for
        # a backstop that (per the field's own docstring) is not
        # expected to fire under real usage.
        self._evict_history_content_over_cap(current_chain_id=chain_id)
        return block

    def is_open_turn_file(self, path: Path, *, current_chain_id: str) -> bool:
        """#5387: True if ``path`` was written by the SAME chain that is
        triggering GC right now — "the turn currently in flight" (GC is
        writer-triggered: see :meth:`_evict_history_content_over_cap`'s
        own docstring for why the triggering write's own ``chain_id`` IS
        "now"). ``not_open_turn(path) = recorded_chain(path) !=
        current_chain`` (architect/owner design, #5387) — this is that
        predicate's negation.

        Deliberately requires BOTH a real ``current_chain_id`` (empty ==
        "no chain known for this GC pass" == protects nothing) AND a
        recorded entry for ``path`` (absent == "this file predates
        #5387's wiring, or its own write never got a chain_id" == never
        open-turn) — a file can only be excluded from GC by matching an
        ACTUAL chain, never by an absence on either side coincidentally
        comparing equal (``"" == ""`` would otherwise protect every
        chainless file against every chainless GC trigger)."""
        if not current_chain_id:
            return False
        return self._chain_by_path.get(path) == current_chain_id

    def _evict_history_content_over_cap(self, *, current_chain_id: str = "") -> None:
        """#5364 §1.6 "C" / #5387 "L": delete this session's own OLDEST
        history-content files (see :func:`_eviction_order`) until its
        directory is back under ``history_content_max_bytes`` — SKIPPING
        any file :meth:`is_open_turn_file` says belongs to the turn that
        triggered THIS pass (default: protected; see
        ``MediaStoreConfig.protect_open_turn_from_gc`` for the opt-in to
        disable this and evict open-turn content too). Scoped to THIS
        session's own subdirectory, not the whole ``history_content_root``
        — the cap's own subject is one session's content (see the field's
        docstring: cross-session growth is #5366's separate subject, not
        this one's). Best-effort: an ``OSError`` mid-delete is logged and
        skipped, same policy as :meth:`_purge_session_dir`'s own sibling
        deletes — a failed eviction must never fail the write it
        followed.

        #5387 scope (architect design B, stated explicitly — NOT a
        decision to leave a gap, a reach limit): this only protects the
        write that triggered THIS session's own GC pass. Another
        session's own open turn is out of C's reach entirely — C only
        ever enumerates THIS session's own directory — and stays
        unprotected only until a CROSS-session GC (#5366) is built; #5366
        is where that would first need to be handled, not here.

        Disclosed (architect review, non-blocking): if EVERY remaining
        candidate is protected (all share the triggering chain), this
        returns having deleted nothing, and the directory stays OVER
        cap — deliberately: protecting an open turn's content is worth
        more than strictly enforcing the byte cap on any given pass.
        Not silent — the caller (:meth:`save_tool_result`) already logs
        nothing extra here because this is the expected, harmless
        common case (see ``history_content_max_bytes``'s own docstring:
        2 GB default, eviction is not expected to fire under real usage
        at all); a future lower cap that fires routinely against a
        single long-running chain would see this over-cap state
        persist across many writes, not a one-pass fluke."""
        cap = self._config.history_content_max_bytes
        directory = self._history_content_dir()
        _, total = _dir_stats_recursive(directory)
        if total <= cap:
            return
        protect_open_turn = self._config.protect_open_turn_from_gc
        for path in _eviction_order(directory):
            if total <= cap:
                break
            if protect_open_turn and self.is_open_turn_file(
                path, current_chain_id=current_chain_id,
            ):
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as e:  # noqa: BLE001 — best-effort; LOG (don't silently swallow)
                logger.warning(
                    "#5364 §1.6: eviction of %s under the history-content "
                    "cap failed: %s",
                    path, e,
                )
                continue
            total -= size

    @property
    def durability_failed(self) -> bool:
        """#5364 §1.5: True once a fire-and-forget ``save_tool_result``
        write failed PERSISTENTLY (§4-exhausted retries) — the SAME
        latched health-signal ``DurabilityWorker.durability_failed``
        exposes, delegated here so :meth:`save_tool_result` itself can
        refuse a NEW write once this is known, rather than minting a ref
        for a file the worker has already given up trying to write.
        Never auto-clears (mirrors the worker's own contract)."""
        return self._worker.durability_failed

    def _submit_write_or_inline(self, do_write) -> None:
        """#5364 §1.4: ``DurabilityWorker.submit_nowait`` requires a RUNNING
        event loop (it binds its queue to one) — but ``save_tool_result``
        has always been callable from a plain synchronous context (a CLI
        script, ``reyn doctor``, a sync test with no ``asyncio.run``
        wrapper), and every real chat turn's own call is already inside one
        (``RouterLoop.feedback`` runs on ``_run_execute_round``'s live
        loop). Rather than making every sync caller responsible for
        spinning one up, this checks: a loop is running → defer via the
        worker (the off-loop win this section exists for); no loop running →
        run ``do_write`` inline via ``asyncio.run`` (byte-identical to the
        pre-#5364-§1.4 synchronous write — a script/test never sees this
        move as a behaviour change, only chat turns do)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(do_write())
        else:
            self._worker.submit_nowait(do_write)

    async def flush(self) -> None:
        """#5364 §1.4: wait until every enqueued :meth:`save_tool_result`
        write has actually landed on disk — WITHOUT closing the worker.

        This is the ONE barrier the fire-and-forget write above relies on:
        the router loop calls this once per turn, right before the NEXT LLM
        call (never per-write — see ``router_loop.py``'s own call site) —
        so by the time a ``read_file(path=<ref>)`` tool call could possibly
        reach the model (it can only follow a completion the model has
        already seen the ref in), the write behind that ref is guaranteed
        durable. Mirrors ``DurabilityWorker.flush``'s own contract exactly
        (this store just owns a dedicated worker instance — see
        :meth:`__init__`)."""
        await self._worker.flush()

    async def aclose(self) -> None:
        """#5364 §1.4 (same class of gap #2783 named for ``EventStore``,
        #4961 C for ``_audit_events`` — a 3rd instance): drain this store's
        worker before the process exits. Without this, a normal session
        shutdown can drop a still-queued ``save_tool_result`` write —
        ``asyncio.run`` cancels outstanding tasks at loop teardown and this
        write is fire-and-forget by construction. Called from the
        registry's session-teardown seams alongside
        ``aclose_event_store``/``aclose_audit_events`` — same pattern, same
        call sites (``Session.aclose_media_store``). Idempotent
        (``DurabilityWorker.aclose`` is idempotent)."""
        await self._worker.aclose()

    def read_tool_result(self, path_str: str) -> tuple[str, bool]:
        """Read tool result text by project-relative path.

        Validates the resolved path lives inside EITHER the pre-#5364
        ``tool_results_dir`` (old-format path-refs, never migrated — #5364
        §1.1 "移行scriptは要らない") OR the current
        ``history_content_root`` (new writes, #5364 §1.1). Returns
        ``(text, found)``.

        #5364 §1.2: the resolver opens the entry's OWN path as recorded —
        it never reconstructs one from a base dir + filename (doing so
        would make every pre-#5364 entry resolve to a location it was
        never written to, i.e. universally ``lost``). This method's job
        is only to pick the matching validation boundary for whichever
        directory the path actually resolves under; it never rewrites the
        path itself.

        The LOCAL read is delegated to :func:`read_offloaded` from
        ``services/offload/store.py`` (Phase 2 of the offload-dedup effort).
        The ``(text, found)`` contract and ``PermissionError`` boundary are
        preserved identically — callers are unaffected.
        """
        # Resolve project-relative → absolute before calling read_offloaded,
        # which validates against a single base_dir.
        abs_path = (self._project_root / path_str).resolve()
        for base_dir in (self._history_content_root, self._tool_results_dir):
            try:
                abs_path.relative_to(base_dir.resolve())
            except ValueError:
                continue
            return read_offloaded(str(abs_path), base_dir=base_dir)
        # Neither boundary contains this path — same PermissionError shape
        # as before #5364 (existing consumers match on "outside
        # tool_results_dir"), extended to name both valid locations.
        raise PermissionError(
            f"path {path_str!r} is outside both tool_results_dir "
            f"{self._tool_results_dir} and history_content_root "
            f"{self._history_content_root} — refusing to read"
        )

    # ── Cross-host routing (#385 β core impl sub-task 1) ──────────────

    def _attach_cross_host_fields(
        self, block: dict, *, filename: str, chain_id: str,
    ) -> None:
        """Augment a path-ref block with resource_uri / source_agent /
        source_chain_id when this store has an ``agent_name`` identity.

        No-op when ``agent_name`` is unset — leaves the block in the
        pre-β shape so legacy call sites and test stubs keep working
        with their original expectations. The added fields are purely
        additive; the ``path`` fast-path stays usable for same-host
        consumers regardless.

        When ``base_url`` is also set (= #385 β sub-task 3b), augments
        further with a ``url`` field — the HTTP fetch URL for cross-
        host consumers. Without ``base_url`` only ``resource_uri`` is
        emitted (= vendor-scheme identifier, no fetch location).
        """
        if not self._agent_name:
            return
        block["resource_uri"] = f"{_RESOURCE_URI_SCHEME}{self._agent_name}/{filename}"
        block["source_agent"] = self._agent_name
        if chain_id:
            block["source_chain_id"] = chain_id
        if self._base_url:
            block["url"] = (
                f"{self._base_url}/agents/{self._agent_name}"
                f"/tool-results/{filename}"
            )

    def read_tool_result_by_url(self, url: str) -> tuple[str, bool]:
        """Resolve an ``https://.../agents/<agent>/tool-results/<artifact>``
        URL and read the body when it points back to this Reyn instance.

        Used by the cross-host dispatcher (= #385 β core impl
        sub-task 3c) as the same-host short-circuit: when the URL host
        matches this store's ``base_url``, we read the file via the
        same ``read_tool_result`` path instead of making a real HTTP
        round-trip. Saves a network hop and keeps debugging simple.

        Raises ``ValueError`` when:
          - ``base_url`` is unset (= can't compare hosts)
          - URL host doesn't match local (= caller should HTTP GET instead)
          - URL path doesn't match the expected
            ``/agents/<agent>/tool-results/<artifact>`` shape
        """
        if not self._base_url:
            raise ValueError(
                "this MediaStore has no base_url configured; "
                "cannot determine whether the URL is local"
            )
        if not url.startswith(self._base_url + "/"):
            raise ValueError(
                f"URL host does not match local base_url; "
                f"caller should HTTP GET (cross-host): {url!r}"
            )
        tail = url[len(self._base_url):]
        # Expected: /agents/<agent>/tool-results/<artifact>
        parts = tail.lstrip("/").split("/")
        if len(parts) < 4 or parts[0] != "agents" or parts[2] != "tool-results":
            raise ValueError(
                f"URL path does not match /agents/<agent>/tool-results/<artifact>: {url!r}"
            )
        artifact = "/".join(parts[3:])
        candidate = self.find_tool_result_artifact(artifact)
        if candidate is None:
            return "", False
        # Re-use the existing path-based reader (= same boundary check).
        rel_path = str(candidate.relative_to(self._project_root))
        return self.read_tool_result(rel_path)

    def read_tool_result_by_uri(self, uri: str) -> tuple[str, bool]:
        """Resolve a ``reyn-tool-result://...`` URI and read the body.

        Same-host case (= the URI's source_agent matches this store's
        ``agent_name``): the artifact portion is interpreted as a filename
        inside ``tool_results_dir`` and read like ``read_tool_result``.

        Cross-host case (= source_agent differs): raises ``ValueError``
        with a "cross-host not yet supported" message. The actual RPC
        routing lands in sub-task 3 of the #385 β core impl; this
        sub-task's contract is the schema + same-host resolution + a
        clear stub error for the dispatcher to surface.

        Malformed URI: raises ``ValueError`` with the offending input.
        Missing file: returns ``("", False)`` matching
        ``read_tool_result``'s past-EOF / deleted-file convention.
        """
        parsed = parse_resource_uri(uri)
        if parsed is None:
            raise ValueError(
                f"invalid resource_uri (expected "
                f"{_RESOURCE_URI_SCHEME}<agent>/<artifact>): {uri!r}"
            )
        agent, artifact = parsed
        if not self._agent_name:
            raise ValueError(
                "this MediaStore has no agent_name configured; "
                "cannot resolve cross-host resource URIs"
            )
        if agent != self._agent_name:
            raise ValueError(
                f"cross-host resource_uri (source_agent={agent!r}) is not "
                "yet supported in this build (= sub-task 3 of #385 β core "
                f"impl). This store's identity is {self._agent_name!r}."
            )
        # Same-host: the artifact is a filename inside either this
        # session's history-content directory (#5364, current writes) or
        # the pre-#5364 tool_results_dir (legacy, read-only). Try the
        # current location first (the overwhelming common case — a
        # same-process round trip), falling back to the legacy one so an
        # OLD resource_uri (minted before #5364) still resolves.
        # Delegate to read_tool_result with the project-relative path so
        # workspace-boundary validation runs through the existing path.
        candidate = self.find_tool_result_artifact(artifact)
        if candidate is None:
            return "", False
        rel_path = str(candidate.relative_to(self._project_root))
        return self.read_tool_result(rel_path)

    def find_tool_result_artifact(self, artifact: str) -> "Path | None":
        """#5364: locate an on-disk artifact by FILENAME ONLY (the shape
        every same-host cross-reference — resource_uri / url — carries,
        none of which encode a session_id) across every valid location,
        in order: (1) THIS store's own session directory (the overwhelming
        common case — a same-process round trip); (2) every OTHER
        session's subdirectory under ``history_content_root`` (a
        different session's write, read back through THIS store); (3)
        the legacy flat ``tool_results_dir`` (pre-#5364, read-only). The
        first existing match wins; ``None`` when nowhere has it."""
        own_session = self._history_content_dir() / artifact
        if own_session.exists():
            return own_session
        if self._history_content_root.is_dir():
            other_session = next(
                (p for p in self._history_content_root.rglob(artifact) if p.is_file()),
                None,
            )
            if other_session is not None:
                return other_session
        legacy = self._tool_results_dir / artifact
        if legacy.exists():
            return legacy
        return None

    @property
    def agent_name(self) -> str | None:
        """Agent identity bound to this store (= source_agent for path-refs).

        ``None`` when the store was constructed without an identity (=
        legacy / test stubs). Consumers that need to render cross-host
        capable path-refs MUST construct the store with this set.
        """
        return self._agent_name

    # ── Introspection ─────────────────────────────────────────────────

    @property
    def media_dir(self) -> Path:
        """Absolute path of the image storage directory."""
        return self._media_dir

    @property
    def tool_results_dir(self) -> Path:
        """Absolute path of the PRE-#5364 tool result text storage
        directory — read-only going forward; see :meth:`read_tool_result`.
        """
        return self._tool_results_dir

    @property
    def history_content_dir(self) -> Path:
        """#5364: absolute path of THIS session's tool-result write
        directory (``.reyn/memory/history-content/<session_id>/``) — where
        :meth:`save_tool_result` writes now."""
        return self._history_content_dir()

    def storage_stats(self) -> "MediaStorageStats":
        """#4478 Phase 1: a read-only, policy-independent snapshot of this
        store's on-disk footprint. Never deletes, never evicts — this is
        the measurement surface the module docstring's own "Phase 2
        reservation" names as the precondition for any future TTL/max-N/
        session-end policy ("trigger is measurement evidence, not
        hypothesis"). Mirrors the chat presenter's own public
        ``image_cache_size_bytes``-style snapshot-read pattern (#4376),
        applied here to on-disk rather than in-memory storage."""
        media_count, media_bytes = _dir_stats(self._media_dir)
        # #5364: tool-result writes moved to history_content_root (nested
        # per session) — tool_result_file_count/bytes measure the CURRENT
        # write location, same field name/meaning as before the move
        # (the legacy .reyn/tool-results/ tree is frozen going forward,
        # so continuing to report it here would make this measurement
        # surface silently stop reflecting new writes).
        tr_count, tr_bytes = _dir_stats_recursive(self._history_content_root)
        return MediaStorageStats(
            media_file_count=media_count,
            media_bytes=media_bytes,
            tool_result_file_count=tr_count,
            tool_result_bytes=tr_bytes,
        )
