#!/usr/bin/env python3
"""#5267 (Family A) — a static gate against a raw ``asyncio.create_task``
call inside a module that OWNS a :class:`~reyn.runtime.tracked_tasks.
TrackedTaskSet`, bypassing the single funnel #4759 built for exactly this
("SpawnTracker and OutboxHub made task_tracker a REQUIRED constructor
param ... skipping the funnel there is now a TypeError, not a silent
gap" — but, per that same issue's own words, "where it's optional, it is
still a discipline, not a guarantee" for every OTHER caller).

## The failure this closes

#5267's own chain: ``Session.run_one_iteration()`` spawns a turn's body
via a bare ``asyncio.create_task`` (``session.py``), never through
``TrackedTaskSet``. ``Task.cancel()`` only requests cancellation at the
task's current await point — it does not, and cannot, stop a background
thread (or any other still-running work) a cancelled task's own
``finally`` may still be spawning/writing through. A turn cancelled
mid-flight can leave background work racing the NEXT turn over the same
mutable session state, with nothing red anywhere: cancellation is a
human action (Ctrl-C, a slash command), not something a test suite
presses on its own. #5268 (same night) found a second, independent
instance of the identical shape in ``llm.py``'s own ad hoc task pair.

The fix this gate exists to make structural (not a one-off patch) is
routing every such spawn through ``TrackedTaskSet.spawn()`` instead of a
bare ``asyncio.create_task`` — the funnel already awaits its own tasks'
cancellation properly (``aclose()``'s ``asyncio.gather(..., return_
exceptions=True)``), so a module that spawns exclusively through it gets
that discipline for free, the same way #4759 already made it a
``TypeError`` (not a silent gap) to construct ``SpawnTracker``/
``OutboxHub`` without one.

## Scope — derived, not enumerated by hand

A hand-typed module list is a census: "the modules I noticed today", not
"the modules that are actually exposed" — a 9th case that arrives via a
FUTURE PR, adding a module that owns a ``TrackedTaskSet``, would silently
sit outside a frozen list forever (nothing here would turn red to say
so). Scope is derived from two independent, both mechanical, ownership
shapes — verified against the real tree the night this gate was written
(#5267 comment thread: the first draft, "required constructor param"
only, undercounted to 2 files and structurally EXCLUDED ``session.py`` —
the very file #5267's own chain names — because ``Session`` does not
RECEIVE a ``TrackedTaskSet``, it CONSTRUCTS one):

1. **Receives one as a required constructor param** — an ``__init__``
   parameter whose annotation names ``TrackedTaskSet`` with no
   ``None``-tolerant union and no default value (``outbox_hub.py``,
   ``spawn_tracker.py`` today). ``ChainManager`` deliberately keeps this
   OPTIONAL (``TrackedTaskSet | None = None``, documented in its own
   ``__init__`` — 12 pre-existing test call sites never exercise #4759's
   teardown property, and forcing all 12 to thread a tracker through
   would touch files unrelated to any single PR's own concern) — it is
   correctly OUT of this gate's scope; see that constructor's own
   docstring for the exact condition under which it would need to
   re-enter scope.
2. **Constructs one itself** — a ``TrackedTaskSet(...)`` call anywhere in
   the module (``session.py``'s own ``self._background_tasks =
   TrackedTaskSet()``). Construction is the strongest form of ownership;
   a module that builds the funnel and then bypasses it for some OTHER
   spawn is the exact defect #5267 found, so this criterion is what
   brings ``session.py`` itself into scope.

Both are AST-derived facts about the file's own text, not a judgment
call — a module that starts receiving/constructing a ``TrackedTaskSet``
tomorrow enters scope automatically; one that stops (drops the param,
inlines a different task owner) leaves it automatically. ``tracked_
tasks.py`` itself is excluded unconditionally — it IS the funnel;
``TrackedTaskSet.spawn()``'s own ``asyncio.create_task`` call is the
one legitimate call site this whole gate exists to fence everything
else away from.

## Warn-only (architect ruling, #5267) — promotion condition stated now

This gate starts at WARN (the CI workflow always exits 0; see
``.github/workflows/task-funnel-bypass-gate.yml``) because nobody has
measured this specific AST shape's false-positive rate against the real
tree yet. #5010's own history is the reason a promotion condition is
written HERE, NOW, rather than left for later: #5010 stayed warn-only
indefinitely because nobody wrote down what "ready to promote" meant,
so there was never a moment anyone could point to and say "now" — it
just accumulated as a gate that looks like it protects something while
promoting nothing.

**Promotion condition**: 0 new false positives, across 20 consecutive
PRs that touch an in-scope file, promotes this to a blocking (error-exit)
gate. (20, not a time window — a time-based count depends on this
project's own PR pace, which this gate has no reason to track; 20 is
enough PRs to hit the in-scope files, which are a small, named-by-
construction subset of ``src/``, several times over.)

**Removal condition**: if 2 weeks pass with the promotion condition
still unmet (i.e. a false positive fired within that window), this gate
is a candidate for removal rather than indefinite warn status — a
warn-only gate nobody ever promotes is indistinguishable, in effect,
from no gate at all, and #5010 is the standing example of exactly that
non-outcome.

**Who counts, and how**: a human, at promotion-decision time — not a
new script. ``git log -p -- scripts/task_funnel_bypass_baseline.json``
shows every commit that added an entry; the commit(s) adding a
``"false_positive"`` entry ARE the false-positive count, in order,
against the PRs that touched an in-scope file over that stretch — a
commit that adds an ``"accepted"`` entry (#5300; see "The baseline"
below) does NOT count, since that type means the gate correctly found a
real bypass, not that the gate was wrong. #5010 stalled because nobody
wrote down what "ready to promote" meant, not because nothing counted
it automatically — writing the one-line derivation above, at promotion
time, is enough; building a counter for it would be solving a problem
#5010 never actually had. ``false_positive_count()`` below exposes the
CURRENT baseline's own count mechanically (for tests and for a quick
sanity check at promotion time) but does not replace the git-log walk
above, which is the only thing that can distinguish an entry added THIS
stretch from one carried over from before it.

## The baseline — where a false positive gets COUNTED (lead-coder review)

The 20-PR/2-week conditions above need a denominator: something that
actually COUNTS a false positive when one fires, distinct from a real
defect. The first draft's own test (a bare "the tree has exactly this
one disclosed hit" pin) failed that job — its own failure message told
a person to "update this pin," which silently absorbs a genuinely NEW
hit (false positive OR real defect, indistinguishable) into the pin
with zero record that anything happened. Zero false positives could
ever get counted that way, so the 20-PR promotion condition could never
be evaluated either — not because none occurred, but because nothing
was built to notice.

``task_funnel_bypass_baseline.json`` is the fix — the same
declared-baseline idiom this repo already uses for exactly this problem
(``mypy_ratchet.py``'s ``(file, code)`` pairs, ``flat_tests_ratchet.py``'s
filename set): every CURRENTLY accepted offender is a declared entry,
keyed by file path, each carrying its own ``"type"`` and a ``"note"``
explaining which and why. Three types exist, and they carry three
DIFFERENT meanings — collapsing any two of them into one label would
mix "the gate is wrong" into "the gate is right and this is fine",
silently contaminating the promotion condition's own count (#5300):

- ``"defect"`` — the bypass is real AND dangerous; a real hazard,
  tracked for its own fix PR.
- ``"false_positive"`` — the gate is WRONG about this one; not a
  bypass at all (a scope/AST-shape miss). This is the ONLY type
  ``false_positive_count()`` counts, and the only type the promotion
  condition's "0 new false positives" reads.
- ``"accepted"`` (#5300) — the bypass is real, but was MEASURED and
  judged not dangerous — the gate correctly found a real funnel-bypass;
  the danger it warns about specifically does not apply here. Does NOT
  count toward the false-positive rate: the gate was right to flag it,
  a human just decided the flagged thing is fine. The note MUST say
  what was measured (not just "seems fine") — ``session.py``'s own
  entry is the model for this.

``session.py`` was this file's first entry, typed ``"defect"``; #5267
measured the specific orphaning hazard it was flagged for and could not
reproduce it, so it moved to ``"false_positive"`` (#5297) — a
misclassification later found: the *gate* was not wrong (the bypass —
a raw ``asyncio.create_task`` outside the funnel — is real), only the
*danger* judgment was. #5300 re-types it a second time, to
``"accepted"``, which is the type that actually matches what #5267
measured. The test
(``tests/scripts/test_check_task_funnel_bypass_5267.py``) asserts the
REAL tree's offenders match the declared set exactly; a new,
undeclared offender fails that test (a real, already-blocking CI signal
via the normal pytest job — no separate always-blocking workflow
needed), forcing whoever introduced it to add a declared entry and
classify it BEFORE the PR is green. That one required action is what
makes ``"false_positive"`` entries countable: they are not a "same as
before" default that requires nobody to write anything down.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _ROOT / "src" / "reyn"
_TRACKED_TASKS_MODULE = _SRC_DIR / "runtime" / "tracked_tasks.py"
_BASELINE_PATH = _ROOT / "scripts" / "task_funnel_bypass_baseline.json"

# The three baseline entry types (#5300) — see "The baseline" in this
# module's own docstring for what each means and why collapsing any two
# into one label would contaminate the promotion condition's own count.
TYPE_DEFECT = "defect"
TYPE_FALSE_POSITIVE = "false_positive"
TYPE_ACCEPTED = "accepted"
VALID_BASELINE_TYPES = frozenset({TYPE_DEFECT, TYPE_FALSE_POSITIVE, TYPE_ACCEPTED})


def _annotation_text(node: "ast.expr | None") -> str:
    """Best-effort source text of an annotation node — this repo writes
    these as quoted forward refs (``"TrackedTaskSet"`` / ``"TrackedTaskSet
    | None"``) as often as bare expressions, so a plain ``ast.unparse``
    (which would just re-quote the string constant) is not enough; peel
    one layer of ``ast.Constant`` string first."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — best-effort only, never fatal
        return ""


def _is_required_tracked_task_set_param(arg: ast.arg, has_default: bool) -> bool:
    """True if *arg* is annotated as (a non-Optional) ``TrackedTaskSet``
    and has no default value — criterion ① in the module docstring."""
    text = _annotation_text(arg.annotation)
    if "TrackedTaskSet" not in text:
        return False
    if "None" in text or "Optional" in text:
        return False  # None-tolerant — e.g. ChainManager's own param
    return not has_default


def _init_has_required_tracker_param(cls: ast.ClassDef) -> bool:
    for node in cls.body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "__init__"):
            continue
        args = node.args
        # Positional-or-keyword args: only the tail (len(defaults)) have
        # defaults; align from the right, same as Python's own binding.
        posargs = args.args
        pos_defaults = [None] * (len(posargs) - len(args.defaults)) + list(args.defaults)
        for a, d in zip(posargs, pos_defaults):
            if _is_required_tracked_task_set_param(a, d is not None):
                return True
        # Keyword-only args: aligned 1:1 with kw_defaults (None entry = no default).
        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            if _is_required_tracked_task_set_param(a, d is not None):
                return True
    return False


def _module_constructs_tracked_task_set(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if name == "TrackedTaskSet":
            return True
    return False


def _is_raw_create_task_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "create_task":
        return isinstance(func.value, ast.Name) and func.value.id == "asyncio"
    if isinstance(func, ast.Name) and func.id == "create_task":
        return True  # `from asyncio import create_task` shape
    return False


def in_scope_files(src_dir: Path) -> "list[Path]":
    """Every file that OWNS a ``TrackedTaskSet`` (criterion ① or ②),
    excluding the funnel's own implementation file — the mechanical scope
    this gate's own module docstring describes. Directly testable in
    isolation from the offender scan below."""
    scoped: "list[Path]" = []
    for path in sorted(src_dir.rglob("*.py")):
        if path == _TRACKED_TASKS_MODULE:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        if _module_constructs_tracked_task_set(tree):
            scoped.append(path)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _init_has_required_tracker_param(node):
                scoped.append(path)
                break
    return scoped


def offending_files(src_dir: Path) -> "list[tuple[Path, list[int]]]":
    """The gate's whole decision, isolated from CLI/printing — directly
    testable, mirrors ``check_cancel_swallow.py``'s own split."""
    offenders: "list[tuple[Path, list[int]]]" = []
    for path in in_scope_files(src_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = sorted(
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_raw_create_task_call(node)
        )
        if hits:
            offenders.append((path, hits))
    return offenders


def load_declared_baseline(path: Path = _BASELINE_PATH) -> "dict[str, dict[str, str]]":
    """The committed record of every offender CURRENTLY accepted, keyed by
    a ``src/reyn/...``-relative path string, each carrying its own
    ``type`` (``"defect"`` / ``"false_positive"`` / ``"accepted"``) and
    ``note`` — see this module's own "The baseline" docstring section for
    why this exists instead of a bare pinned count, and for what each of
    the three types means."""
    return json.loads(path.read_text(encoding="utf-8"))


def false_positive_count(baseline: "dict[str, dict[str, str]]") -> int:
    """Count only the ``"false_positive"``-typed entries in *baseline* —
    the promotion condition's own denominator (#5300). ``"accepted"``
    entries (a real bypass, measured and judged not dangerous) and
    ``"defect"`` entries (a real hazard, tracked for its own fix PR) must
    NOT inflate this count: only a case where the GATE ITSELF was wrong
    counts against the gate's own precision. See "Who counts, and how" in
    this module's own docstring — this function is the current-baseline
    snapshot that derivation reads; it does not replace the git-log walk
    needed to isolate one promotion stretch's own additions."""
    return sum(1 for entry in baseline.values() if entry.get("type") == TYPE_FALSE_POSITIVE)


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-tree scan against a derived scope
    offenders = offending_files(_SRC_DIR)

    if not offenders:
        print(
            "OK: no raw asyncio.create_task() call found inside a module "
            "that owns a TrackedTaskSet."
        )
        return 0

    print("task-funnel-bypass gate FAILED (WARN-ONLY — see module docstring):\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) own a TrackedTaskSet (receive one as a "
        "required constructor param, or construct one directly) but spawn "
        "at least one background task with a raw asyncio.create_task() "
        "instead of routing it through TrackedTaskSet.spawn() (#4759) — "
        "such a task is invisible to the funnel's own teardown/cancel-"
        "then-await discipline (#5267):",
        file=sys.stderr,
    )
    for path, hits in offenders:
        rel = path.relative_to(_ROOT)
        for line in hits:
            print(f"  {rel}:{line}: raw asyncio.create_task() in a TrackedTaskSet-owning module", file=sys.stderr)

    print(
        "\nFix: route the spawn through this module's own TrackedTaskSet "
        "instance's .spawn() instead of asyncio.create_task() directly "
        "(see outbox_hub.py / spawn_tracker.py for the accepted shape).\n"
        "\nWarn-only for now — promotion/removal conditions are in this "
        "script's own module docstring.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
