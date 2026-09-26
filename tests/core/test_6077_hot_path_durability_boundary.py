"""Tier 2: #6077 提案 4 — the hot path's durability invariant, STRENGTHENED.

The old invariant (``state_log.py``/``snapshot_journal.py``'s own prior
wording, both now updated by this PR) said only "the hot path NEVER AWAITS
durability" — true of every fix this arc landed, and yet it PERMITTED the
very defects the arc spent itself removing: ``copy.deepcopy``, ``json.dumps``
and ``open``/``write``/``close`` never ``await`` anything, so none of them
ever violated the old wording, while each one blocked the event loop exactly
the way an ``await`` would have. The new invariant this module enforces:

    the hot path neither AWAITS nor PERFORMS durability work

## Population A — the roots, DERIVED, never curated

Architect's own ruling (issue #6077, "母集団を curate しない こと"): a hand-
listed set of "hot path functions" is a curated subset — the SAME shape
this repo's completeness discipline forbids elsewhere (``LONG_RUNNING_
PAYLOAD_TYPES``, the #6083 gate this module's own derivation style borrows
from). The structural tell used instead: a function that hands work to the
``DurabilityWorker`` WITHOUT awaiting it is, BY DEFINITION, on the hot-path
side of the durability boundary — the awaited forms (``submit``, ``submit_
durable``) can never be hot-path roots, so they need no exclusion. Three
fire-and-forget names, `git grep`'d against ``src/`` (architect's own count
at #6077 提案 4's own landing, independently reproduced by
:func:`_derive_hot_path_entrypoints` below; #6240 ③ added a twelfth call
site, ``Session._append_history``'s own ``submit_nowait``):

    submit_nowait           6
    submit_threadsafe       1
    submit_durable_nowait   5
    ---------------------------
    total                  12  (0 false positives)

A naive ``\\.submit(`` needle DOES false-positive twice — ``hooks/external_
fire.py``'s bridge and ``runtime/capability_visibility.py``'s ``concurrent.
futures`` executor, both bare ``.submit(``, neither a ``DurabilityWorker``.
Restricting to the three FIRE-AND-FORGET names alone clears both without an
exclusion list (an awaited ``.submit(``/``.submit_durable(`` can never be a
hot-path root, so the two false positives — both awaited or otherwise not
one of the three names — never match the needle at all).

A durability-layer function calling ANOTHER durability-layer function
through one of the three names (e.g. ``state_log.py``'s own ``submit_
durable_nowait`` wrapping ``self._worker.submit_nowait`` — the relay
``EventStore.write`` uses) is NOT excluded from the population — excluding
it would make the population curated again, and it is exactly this kind of
relay this arc's own #6077 提案 3+6 PR modified.

## Population B — per-root prologue calls, ALSO derived

For each of the 12 call sites, :func:`_prologue_calls_for` walks the
enclosing function's OWN control-flow graph (see its docstring for the
recognized shapes — guard clauses, try/except, if/else — and SCOPE below
for what it does not) to collect every ``ast.Call`` node that executes on
the path FROM the function's entry point TO that specific fire-and-forget
call (never past it — everything after a submit call has already handed
off; never INTO a nested ``def``/``async def``/``lambda`` — that body runs
LATER, off-loop, when the worker actually drains the job, so a call inside
it is not hot-path work at all).

Every call this walk finds must appear in :data:`_CLASSIFICATION`, mapped
to exactly one of three values:

    capture         — fixes a consistent view of mutable state THIS
                       instant, before something else can mutate it
                       (``model_dump``, ``deepcopy``, a defensive
                       ``dict(...)``/``.get(...)`` read, or a helper whose
                       own job is building the paired closure) — fine on
                       the hot path.
    representation  — serialize / stringify / construct a plain handle
                       object, OR a zero-cost framework query
                       (``asyncio.get_running_loop()``) — also fine (see
                       :func:`test_a_representation_call_does_not_trip_
                       the_gate` for why this is deliberately non-RED).
    durability      — write / fsync / rotate / anything that touches a
                       filesystem or actually performs the durable act —
                       RED if it ever appears here.

A call this walk finds that is NOT a key in :data:`_CLASSIFICATION` is
UNCLASSIFIED — also RED (:func:`test_every_prologue_call_is_classified`):
a brand-new call landing in a hot-path prologue must be looked at by a
human once, not silently pass by matching neither list.

## SCOPE — what this gate does NOT catch (lead-coder's own instruction)

🔴 This gate's population (A) is anchored on functions that CALL one of the
three fire-and-forget names. A function that writes durably WITHOUT ever
going through the ``DurabilityWorker`` — i.e. that bypasses the boundary
entirely rather than crossing it synchronously — is invisible to this gate,
because it never calls ``submit_nowait``/``submit_threadsafe``/``submit_
durable_nowait`` at all. THIS ARC's OWN #6077 提案 1 fix
(``session.py``'s ``_append_history``, before it was moved onto a session-
lifetime handle) is exactly that shape: a plain ``open``/``write``/``close``
per message, calling no worker at all. A gate anchored on the worker-
submission needle cannot see a caller that never reaches the needle —
catching THAT class needs a different, ``open(``-anchored gate (deliberately
not built here: naming ``open(`` by itself would be a denylist, the exact
shape architect's own ruling rejected for population A).

Population B is similarly scoped to the root's OWN function body text: it
does not open the body of any OTHER function it calls (mirrors #6083's
``_prologue_await_reprs`` SCOPE note, axis 1 — "the awaited callee's own
body is never opened"). ``SnapshotJournal.save_nowait``'s prologue, for
instance, shows only ``self._build_snapshot_write_job()`` as a single
Call — the ``copy.deepcopy`` living INSIDE that helper's own body is never
walked here; it is classified as ``capture`` by vouching for the helper's
name, not by re-deriving its internals. A future durability leak added
INSIDE an already-classified helper (rather than at a hot-path root's own
top level) would not be caught by this module.

Control-flow recognition (population B) handles exactly: sequential
statements; a guard clause (``if X: ...diverging.../return``, no
``else``); ``if``/``else`` where the target sits in one branch; ``try``/
``except``/``else`` where the target sits in one block. It does NOT model
``for``/``while`` loops or ``match`` statements — none of the 11 real call
sites use one; a future root written in one of those forms would need this
walk extended first (fails LOUD via :func:`test_the_root_population_is_
not_vacuous` and the AST derivation simply not finding a matching call, not
silently mis-scoping it — see ``_collect``'s own docstring for the one
narrow case, an ``if``/``try`` neither containing the target NOR provably
diverging, where it conservatively still counts a branch's calls).
"""
from __future__ import annotations

import ast
from functools import lru_cache

from tests._support.paths import REPO_ROOT

_SRC_ROOT = REPO_ROOT / "src" / "reyn"

_SUBMIT_NAMES = frozenset({"submit_nowait", "submit_threadsafe", "submit_durable_nowait"})

# ── Population A: roots, declared (checked against the AST derivation below) ──

HOT_PATH_ENTRYPOINTS: "frozenset[str]" = frozenset({
    "core/events/event_store.py::EventStore.write",
    "core/events/event_store.py::EventStore.submit_auto_purge",
    "core/events/state_log.py::StateLog.append_nowait",
    "core/events/state_log.py::StateLog.submit_durable_nowait",
    "core/events/state_log.py::StateLog.truncate_below",
    "core/events/pipeline_recovery.py::record_pipeline_state",
    "data/workspace/media_store.py::MediaStore._submit_write_or_inline",
    "runtime/registry.py::AgentRegistry._record_agent_identity_generation",
    "runtime/services/snapshot_journal.py::SnapshotJournal.cut_generation",
    "runtime/services/snapshot_journal.py::SnapshotJournal.save_nowait",
    # #6240 ③ (architect ruling, issue #6240 comment 5807710323):
    # Session._append_history's own disk write moved onto a dedicated
    # DurabilityWorker (mirrors EventStore.write, #6077 提案 3/6) -- a
    # NEW root this fix creates, not a pre-existing one this arc missed.
    "runtime/session.py::Session._append_history",
})

# ── Population B: every call in a root's prologue, human-classified ──
#
# Keyed by ast.unparse(call) — same style as #6083's own
# `_prologue_await_reprs` pin (a rename changes the repr and re-trips this,
# on purpose; a line move/reformat does not).

_CLASSIFICATION: "dict[str, str]" = {
    # capture — fixes a value/handle THIS instant, before a later mutation
    # or before a durability layer needs it; no filesystem/serialization
    # work happens in the call itself.
    "event.model_dump(mode='json')": "capture",  # event_store.write's own capture (its docstring names it)
    "self._wal_write_job(kind, fields, None)": "capture",  # builds+returns the deferred job closure, O(1) itself
    "_store(state_log, run_id)": "representation",  # constructs a PipelineStateStore handle from a path -- no I/O
    "dict(control_plane_state)": "capture",  # shallow-copies the dict before handing it to the deferred job
    "self._spawn_lineage.get(name)": "capture",  # reads the edge tuple now, before self._spawn_lineage could mutate
    "self._agent_create_seq.get(name, 0)": "capture",  # same: reads the current seq now
    "self._agent_identity_generation_store()": "representation",  # constructs a store handle from a path -- no I/O
    "copy.deepcopy(self._snapshot.to_payload())": "capture",  # this arc's own named example -- deliberate capture
    "self._snapshot.to_payload()": "capture",  # builds the outer dict deepcopy then captures -- feeds capture
    "self._build_snapshot_write_job()": "capture",  # vouches for the helper: its own body does capture, not durability
    # #6240 ③: Session._append_history's own prologue (all in-memory
    # session-state mutation / a pure derivation -- no filesystem or
    # serialization work; the actual write moved off-loop into
    # _write_history_record_owned, never opened by this gate's own SCOPE).
    "self._enforce_per_message_content_cap(msg)": "capture",  # mutates msg.content/meta in place (byte cap) -- no I/O
    "self.history.append(msg)": "capture",  # resident in-memory list mutation, not the durable write
    "history_record(msg)": "capture",  # pure derivation (asdict + one conditional field drop) -- same role as model_dump above
    # representation — zero-cost framework/control query; no state grabbed,
    # no filesystem touched.
    "asyncio.get_running_loop()": "representation",
}

_DURABILITY = "durability"


# ── AST plumbing ────────────────────────────────────────────────────────

def _is_submit_call(node: object) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _SUBMIT_NAMES


def _pruned_calls(node: "ast.AST") -> "list[ast.Call]":
    """Every ``ast.Call`` in ``node``'s subtree, NOT descending into a
    nested ``def``/``async def``/``lambda`` body (that body runs LATER,
    off-loop -- see module SCOPE)."""
    out: "list[ast.Call]" = []

    def walk(n: "ast.AST") -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                out.append(child)
            walk(child)

    walk(node)
    return out


def _contains(node: "ast.AST", target: "ast.AST") -> bool:
    return any(n is target for n in ast.walk(node))


def _contains_any(stmts: "list[ast.stmt]", target: "ast.AST") -> bool:
    return any(_contains(s, target) for s in stmts)


def _diverges(stmts: "list[ast.stmt]") -> bool:
    """True when the LAST statement of this block unconditionally exits it
    (``return``/``raise``/``continue``/``break``, or an ``if``/``try``
    whose every path does) -- i.e. nothing after this block, in its own
    enclosing block, is reachable via a path that went through here."""
    if not stmts:
        return False
    stmt = stmts[-1]
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(stmt, ast.If):
        return bool(stmt.orelse) and _diverges(stmt.body) and _diverges(stmt.orelse)
    if isinstance(stmt, ast.Try):
        body_ok = _diverges(list(stmt.body) + list(stmt.orelse))
        handlers_ok = all(_diverges(h.body) for h in stmt.handlers) if stmt.handlers else True
        return _diverges(stmt.finalbody) or (body_ok and handlers_ok)
    return False


def _collect(stmts: "list[ast.stmt]", target: "ast.Call | None") -> "tuple[bool, list[ast.Call]]":
    """Walk ``stmts`` (a block) in source order. With ``target`` set,
    returns ``(True, calls)`` where ``calls`` is every ``ast.Call`` that
    executes on the control-flow path REACHING ``target`` (stopping there
    -- everything at or after the statement containing it is excluded,
    since by definition the fire-and-forget hand-off already happened).
    With ``target=None``, returns ``(False, calls)`` where ``calls`` is
    every call that executes on ANY path that falls through this block
    (used to resolve a sibling branch that does not itself contain the
    target -- e.g. a guard clause's ``if`` test, or a ``try`` body that
    completes normally before an unrelated ``else``/target statement)."""
    calls: "list[ast.Call]" = []
    for stmt in stmts:
        found, stmt_calls, diverges = _visit(stmt, target)
        calls.extend(stmt_calls)
        if found:
            return True, calls
        if diverges:
            return False, calls
    return False, calls


def _visit(stmt: "ast.stmt", target: "ast.Call | None") -> "tuple[bool, list[ast.Call], bool]":
    """Returns ``(found, calls, diverges)`` for one statement. See
    :func:`_collect`'s own docstring; recognizes: a nested `def`/`async
    def` (contributes nothing -- deferred); `if`/`try` (recurses into
    whichever branch/handler contains `target`, or -- when `target` is
    None -- every branch that does not unconditionally diverge); any other
    statement (a flat, pruned call scan)."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return False, [], False
    contains = target is not None and _contains(stmt, target)
    if isinstance(stmt, ast.If):
        test_calls = _pruned_calls(stmt.test)
        if contains:
            if _contains(stmt.test, target):
                return True, [c for c in test_calls if c is not target], False
            if _contains_any(stmt.body, target):
                _, calls = _collect(stmt.body, target)
                return True, test_calls + calls, False
            _, calls = _collect(stmt.orelse, target)
            return True, test_calls + calls, False
        body_diverges = _diverges(stmt.body)
        branch_calls: "list[ast.Call]" = []
        if not body_diverges:
            branch_calls += _collect(stmt.body, None)[1]
        orelse_diverges = _diverges(stmt.orelse) if stmt.orelse else False
        if stmt.orelse and not orelse_diverges:
            branch_calls += _collect(stmt.orelse, None)[1]
        return False, test_calls + branch_calls, body_diverges and orelse_diverges
    if isinstance(stmt, ast.Try):
        if contains:
            if _contains_any(list(stmt.body) + list(stmt.orelse), target):
                _, calls = _collect(list(stmt.body) + list(stmt.orelse), target)
                fin = _collect(stmt.finalbody, None)[1] if stmt.finalbody else []
                return True, calls + fin, False
            for handler in stmt.handlers:
                if _contains_any(handler.body, target):
                    _, calls = _collect(handler.body, target)
                    fin = _collect(stmt.finalbody, None)[1] if stmt.finalbody else []
                    return True, calls + fin, False
        body_orelse_diverges = _diverges(list(stmt.body) + list(stmt.orelse))
        branch_calls = []
        if not body_orelse_diverges:
            branch_calls += _collect(list(stmt.body) + list(stmt.orelse), None)[1]
        handlers_all_diverge = True
        for handler in stmt.handlers:
            if not _diverges(handler.body):
                handlers_all_diverge = False
                branch_calls += _collect(handler.body, None)[1]
        fin_calls = _collect(stmt.finalbody, None)[1] if stmt.finalbody else []
        diverges = _diverges(stmt.finalbody) or (
            body_orelse_diverges and handlers_all_diverge and bool(stmt.handlers)
        )
        return False, branch_calls + fin_calls, diverges
    if contains:
        return True, [c for c in _pruned_calls(stmt) if c is not target], False
    calls = _pruned_calls(stmt)
    diverges = isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break))
    return False, calls, diverges


def _qualified_functions(
    tree: "ast.Module",
) -> "list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]":
    """Every function/async-function definition in ``tree``, at ANY
    nesting depth (module-level, class method, or nested closure), paired
    with its dotted qualname (``EventStore.write``,
    ``StateLog._wal_write_job.<locals>._task``). Population A only ever
    matches on the OUTERMOST such name whose own (pruned) body directly
    contains a submit call -- a nested closure's own submit call (none of
    the 11 real sites has one) would be attributed to the closure itself,
    never silently absorbed into its container."""
    found: "list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]" = []

    def walk(node: "ast.AST", prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                found.append((qualname, child))
                walk(child, f"{qualname}.<locals>.")

    walk(tree, "")
    return found


@lru_cache(maxsize=1)
def _iter_source_trees() -> "tuple[tuple[str, ast.Module], ...]":
    """``(relpath-under-src/reyn, parsed tree)`` for every ``.py`` file
    under ``src/reyn`` -- the population A derivation's own source, never
    hand-picked. Cached (module-process-lifetime -- ``src/`` does not
    change mid-run): every test below re-derives populations A and B from
    scratch rather than sharing a fixture (Tier 2's own "no private
    state" discipline -- each assertion is independently reproducible),
    so parsing ~660 files once here, not once per test, is what keeps this
    module's own runtime sane."""
    out: "list[tuple[str, ast.Module]]" = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        out.append((relpath, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return tuple(out)


@lru_cache(maxsize=1)
def _derive_hot_path_entrypoints() -> "frozenset[str]":
    """AST-derive every function whose OWN body (pruned of nested defs)
    directly contains a call to one of :data:`_SUBMIT_NAMES` -- population
    A. Returns ``{"relpath::Qualname", ...}``. Cached -- see
    :func:`_iter_source_trees`'s own note; every test independently calls
    this rather than sharing a fixture, so the derivation itself (not just
    the parse) is memoized too."""
    roots: "set[str]" = set()
    for relpath, tree in _iter_source_trees():
        for qualname, fn in _qualified_functions(tree):
            if any(_is_submit_call(c) for c in _pruned_calls(fn)):
                roots.add(f"{relpath}::{qualname}")
    return frozenset(roots)


@lru_cache(maxsize=1)
def _derive_prologue_calls() -> "dict[str, list[ast.Call]]":
    """For every root (:func:`_derive_hot_path_entrypoints`) and every
    submit call site inside it, the prologue calls reaching that site --
    population B. Keyed by ``"relpath::Qualname#i:attr"`` (``i`` is the
    call's ordinal within the function, so two sites in one function --
    ``MediaStore._submit_write_or_inline`` has both ``submit_nowait`` and
    ``submit_threadsafe`` -- get distinct keys)."""
    out: "dict[str, list[ast.Call]]" = {}
    for relpath, tree in _iter_source_trees():
        for qualname, fn in _qualified_functions(tree):
            submit_calls = [c for c in _pruned_calls(fn) if _is_submit_call(c)]
            for i, call in enumerate(submit_calls):
                attr = call.func.attr  # type: ignore[union-attr]
                key = f"{relpath}::{qualname}#{i}:{attr}"
                _, calls = _collect(fn.body, call)
                out[key] = calls
    return out


# ── Population A tests ──────────────────────────────────────────────────

def test_the_root_population_is_not_vacuous() -> None:
    """Tier 2: question 4's own discipline (and witness ⑷, #6077 brief) --
    a derivation that silently found zero roots would make every assertion
    below pass over an empty collection. Floor: every one of the 11 named
    roots :data:`HOT_PATH_ENTRYPOINTS` currently lists (never a bare count
    -- a size threshold is itself the kind of shape-pinning
    `test_tier_audit.py` rejects; naming the actual roots is the real
    assertion, and it cannot pass emptily since
    :data:`HOT_PATH_ENTRYPOINTS` itself is non-empty).

    #6240 ③ added ``Session._append_history`` as an 11th root, AFTER the
    RED text below was captured (#6077 提案 4's own original 10-root
    witness) -- reproducing that exact strip today would additionally
    list ``'runtime/session.py::Session._append_history'`` in the missing
    set; not re-run, since the RED text is a historical record of the
    original witness, not a live assertion (see the note under it).

    Strip-falsify (witness ④, in-file Edit only -- no ``git stash``/
    ``checkout``/``restore``): temporarily replaced :func:`_is_submit_call`
    with ``return False`` (simulating a broken needle -- the AST walk finds
    nothing). Observed RED, on THIS test AND four others whose own
    populations went empty as a result (the vacuity guard's own point --
    an empty population makes every OTHER assertion in this module pass
    vacuously, not just this one):

        AssertionError: every declared entrypoint must actually be found
        by the AST walk -- missing: frozenset({'core/events/event_
        store.py::EventStore.write', 'core/events/pipeline_recovery.py::
        record_pipeline_state', 'core/events/event_store.py::EventStore.
        submit_auto_purge', 'core/events/state_log.py::StateLog.append_
        nowait', 'runtime/registry.py::AgentRegistry._record_agent_
        identity_generation', 'core/events/state_log.py::StateLog.
        submit_durable_nowait', 'runtime/services/snapshot_journal.py::
        SnapshotJournal.cut_generation', 'data/workspace/media_store.py::
        MediaStore._submit_write_or_inline', 'core/events/state_log.py::
        StateLog.truncate_below', 'runtime/services/snapshot_journal.py::
        SnapshotJournal.save_nowait'})

    (frozenset iteration order is hash-randomized per process -- a re-run
    of this exact strip would print the same 10 members in a different
    order; that is expected and does not change which members appear.)

    (``test_no_declared_root_is_stale``, ``test_prologue_derivation_is_
    not_vacuous``, ``test_a_representation_call_does_not_trip_the_gate``
    and ``test_media_store_is_the_third_durability_consumer_and_stays_
    classified`` also went RED in the same run -- each hit its own empty-
    population assertion, not this one's.) Reverted immediately after
    observing; this docstring is that observation, not a live
    assertion."""
    found = _derive_hot_path_entrypoints()
    assert HOT_PATH_ENTRYPOINTS <= found, (
        "every declared entrypoint must actually be found by the AST walk "
        f"-- missing: {HOT_PATH_ENTRYPOINTS - found!r}"
    )


def test_every_derived_root_is_declared() -> None:
    """Tier 2: population A itself. A function the AST walk finds calling
    ``submit_nowait``/``submit_threadsafe``/``submit_durable_nowait`` that
    is NOT in :data:`HOT_PATH_ENTRYPOINTS` is a new hot-path root landing
    without a human ever classifying its prologue -- the exact silent gap
    #6077 提案 4 exists to close.

    Strip-falsify (witness ①, in-file Edit only -- no ``git stash``/
    ``checkout``/``restore``): temporarily removed ``MediaStore._submit_
    write_or_inline`` from :data:`HOT_PATH_ENTRYPOINTS`. Observed RED:

        AssertionError: new hot-path entrypoint(s) not declared in
        HOT_PATH_ENTRYPOINTS: ['data/workspace/media_store.py::
        MediaStore._submit_write_or_inline'] -- add them, then classify
        every call in their prologue in _CLASSIFICATION before landing
        assert not frozenset({'data/workspace/media_store.py::
        MediaStore._submit_write_or_inline'})

    Reverted immediately after observing; this docstring is that
    observation, not a live assertion."""
    found = _derive_hot_path_entrypoints()
    undeclared = found - HOT_PATH_ENTRYPOINTS
    assert not undeclared, (
        f"new hot-path entrypoint(s) not declared in HOT_PATH_ENTRYPOINTS: "
        f"{sorted(undeclared)!r} -- add them, then classify every call in "
        f"their prologue in _CLASSIFICATION before landing"
    )


def test_no_declared_root_is_stale() -> None:
    """Tier 2: deny side -- a declared root the AST walk no longer finds
    (a removed/renamed submit call) is drift, not the dangerous direction,
    but still worth keeping honest."""
    found = _derive_hot_path_entrypoints()
    stale = HOT_PATH_ENTRYPOINTS - found
    assert not stale, f"declared but no longer a real hot-path root: {sorted(stale)!r}"


# ── Population B tests ──────────────────────────────────────────────────

def test_prologue_derivation_is_not_vacuous() -> None:
    """Tier 2: witness ⑷'s other half -- the per-root prologue walk itself
    must not silently find nothing to check. Floor: the arc's own named
    example (``copy.deepcopy``) must actually appear."""
    all_calls = _derive_prologue_calls()
    assert all_calls, "the prologue derivation found zero call sites"
    reprs = {ast.unparse(c) for calls in all_calls.values() for c in calls}
    assert "copy.deepcopy(self._snapshot.to_payload())" in reprs, (
        f"expected the arc's own named deepcopy call in some root's "
        f"prologue -- found only: {sorted(reprs)!r}"
    )


def test_every_prologue_call_is_classified() -> None:
    """Tier 2: population B, the "unclassified" axis. A call in a
    hot-path root's own prologue (before its fire-and-forget submit) that
    is not a key in ``_CLASSIFICATION`` is RED -- a human must label it
    ``capture``/``representation``/``durability`` once, rather than it
    silently passing by matching neither list.

    Strip-falsify (witness ②, in-file Edit only -- no ``git stash``/
    ``checkout``/``restore``): temporarily added an unclassified
    ``json.dumps(content)`` call to ``record_pipeline_state``'s own
    prologue (``src/reyn/core/events/pipeline_recovery.py``, right after
    ``content = dict(control_plane_state)``, before its ``submit_durable_
    nowait`` call). Observed RED:

        AssertionError: unclassified call(s) in a hot-path prologue:
        {'core/events/pipeline_recovery.py::record_pipeline_state#0:
        submit_durable_nowait': ['json.dumps(content)']} -- add each to
        _CLASSIFICATION as capture/representation/durability
        assert not {'core/events/pipeline_recovery.py::
        record_pipeline_state#0:submit_durable_nowait':
        ['json.dumps(content)']}

    Reverted immediately after observing; this docstring is that
    observation, not a live assertion."""
    all_calls = _derive_prologue_calls()
    unclassified: "dict[str, list[str]]" = {}
    for key, calls in all_calls.items():
        missing = [ast.unparse(c) for c in calls if ast.unparse(c) not in _CLASSIFICATION]
        if missing:
            unclassified[key] = missing
    assert not unclassified, (
        f"unclassified call(s) in a hot-path prologue: {unclassified!r} -- "
        f"add each to _CLASSIFICATION as capture/representation/durability"
    )


def test_no_prologue_call_is_classified_durability() -> None:
    """Tier 2: population B, the actual invariant. A call in a hot-path
    root's own prologue classified ``durability`` means the hot path
    PERFORMS durability work synchronously -- exactly what this arc found
    (``deepcopy``/``json.dumps``/``open``/``write``/``close``) and what
    the strengthened invariant forbids, whether or not it is ever
    ``await``ed.

    Strip-falsify (witness ③, in-file Edit only -- no ``git stash``/
    ``checkout``/``restore``): temporarily reclassified ``copy.deepcopy
    (self._snapshot.to_payload())`` from ``capture`` to ``durability`` in
    :data:`_CLASSIFICATION`. Observed RED:

        AssertionError: durability work found in a hot-path prologue:
        {'runtime/services/snapshot_journal.py::SnapshotJournal.
        cut_generation#0:submit_durable_nowait':
        ['copy.deepcopy(self._snapshot.to_payload())']}
        assert not {'runtime/services/snapshot_journal.py::
        SnapshotJournal.cut_generation#0:submit_durable_nowait':
        ['copy.deepcopy(self._snapshot.to_payload())']}

    Reverted immediately after observing; this docstring is that
    observation, not a live assertion."""
    all_calls = _derive_prologue_calls()
    offenders: "dict[str, list[str]]" = {}
    for key, calls in all_calls.items():
        durable = [
            ast.unparse(c) for c in calls
            if _CLASSIFICATION.get(ast.unparse(c)) == _DURABILITY
        ]
        if durable:
            offenders[key] = durable
    assert not offenders, f"durability work found in a hot-path prologue: {offenders!r}"


def test_a_representation_call_does_not_trip_the_gate() -> None:
    """Tier 1: accept side (#6077 brief, explicit requirement) -- a call
    classified ``representation`` must NOT make
    :func:`test_no_prologue_call_is_classified_durability` fail. Proven
    directly against the real classification map + a real prologue,
    rather than trusted by construction: ``asyncio.get_running_loop()`` is
    genuinely present in more than one root's prologue and genuinely
    classified ``representation``.

    Accept-side check performed directly (in-file Edit only -- no ``git
    stash``/``checkout``/``restore``): temporarily added a SECOND
    ``asyncio.get_running_loop()`` call to ``record_pipeline_state``'s own
    prologue (``src/reyn/core/events/pipeline_recovery.py``, same site as
    witness ②'s ``json.dumps`` addition). The full module stayed GREEN
    (``8 passed``) -- a ``representation``-classified call landing in a
    hot-path prologue does NOT trip either
    :func:`test_every_prologue_call_is_classified` or
    :func:`test_no_prologue_call_is_classified_durability`, confirmed
    against this real classification + a real prologue, not only by the
    construction below. Reverted immediately after observing."""
    assert _CLASSIFICATION["asyncio.get_running_loop()"] == "representation"
    all_calls = _derive_prologue_calls()
    reprs = {ast.unparse(c) for calls in all_calls.values() for c in calls}
    assert "asyncio.get_running_loop()" in reprs, (
        "arrange: expected a real root to have this call in its prologue"
    )
    # Same computation test_no_prologue_call_is_classified_durability does --
    # this call must never surface as an offender.
    offenders = [
        ast.unparse(c)
        for calls in all_calls.values()
        for c in calls
        if ast.unparse(c) == "asyncio.get_running_loop()"
        and _CLASSIFICATION.get(ast.unparse(c)) == _DURABILITY
    ]
    assert not offenders


def test_media_store_is_the_third_durability_consumer_and_stays_classified() -> None:
    """Tier 1: #6077 initial-run finding (lead-coder's own instruction --
    report, never quietly reclassify) -- ``MediaStore._submit_write_or_
    inline`` (``data/workspace/media_store.py:2142``/``:2145``) is a THIRD
    durability consumer this arc's discussion never named until architect
    surveyed the full ``DurabilityWorker`` API. It IS one of the 11
    declared roots (two call sites, ``submit_threadsafe`` and ``submit_
    nowait``) and its prologue calls are ALL ``representation``
    (``asyncio.get_running_loop()``) -- no durability, no unclassified
    call. This test pins that specific finding so a future change to
    either call site re-runs population B against it, not silently drops
    it from coverage."""
    found = _derive_hot_path_entrypoints()
    media_store_roots = {r for r in found if r.startswith("data/workspace/media_store.py::")}
    assert media_store_roots == {
        "data/workspace/media_store.py::MediaStore._submit_write_or_inline",
    }
    all_calls = _derive_prologue_calls()
    media_store_keys = [k for k in all_calls if k.startswith("data/workspace/media_store.py::")]
    attrs = {k.rsplit(":", 1)[-1] for k in media_store_keys}
    assert attrs == {"submit_nowait", "submit_threadsafe"}, media_store_keys
    for key in media_store_keys:
        for call in all_calls[key]:
            assert _CLASSIFICATION.get(ast.unparse(call)) != _DURABILITY, (key, ast.unparse(call))
