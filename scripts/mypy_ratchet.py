#!/usr/bin/env python3
"""A mypy finding is a ratchet, not a report — new ones fail CI, old ones don't need closing first.

#3726: mypy has been configured in ``pyproject.toml`` since before this script
existed, and no CI job ever ran it — a declared check with no execution, the
same shape #3024 (env-identity) and #3595 S4 (the slash residue gate) each hit
independently. Two of the day's real defects (`compact_caps` never wired,
`fv.cursor` reading an attribute 0.12.0 dropped) were exactly the class mypy
exists to catch and neither showed up until someone ran it by hand.

The straightforward fix — wire mypy into CI as a blocking gate — does not
work today: a real measurement against the current tree returns 603 errors.
Requiring that backlog closed before adoption defers adoption indefinitely
(every day it stays unadopted is another day a `compact_caps`-shaped defect
can land unnoticed), and a **report-only** job does not close the gap either
— it would just be a second instance of "configured, running, changing
nobody's decision," a report nobody reads is not meaningfully different from
a check nobody runs.

So this is a **ratchet**, the same skeleton as
``tests/interfaces/test_3595_s4_slash_handler_seam.py``'s ``_SESSION_RESIDUE``: a
committed BASELINE of every ``(file, error-code)`` pair the tree currently
carries. A pair not in the baseline is new — CI fails, immediately, the same
day the defect lands. A pair that WAS in the baseline and is gone because
someone fixed it just silently stops appearing; nothing has to be edited to
let a fix "count." The baseline can only ever be read down over time by
actual fixes — regenerating it wholesale to make a new failure disappear is
the one way to defeat the ratchet, and is exactly the file-count-preserving
excuse #3123's own review vocabulary calls out.

Deliberately keyed on ``(file, error-code)``, not the bare per-file error
COUNT: a count-only baseline lets "fix one, introduce a different one" pass
silently (the fixed count and the new count can net to the same total). Keyed
this way, a genuinely NEW finding in an already-baselined file for a
DIFFERENT code is still visible — only the exact ``(file, code)`` pairs
already declared are grandfathered.

The chosen grain has a known, accepted cost (#3738 review): a genuinely NEW
finding of the SAME code in a file that already carries that code is
INVISIBLE — grandfathered under the existing pair, same as a real fix would
be. Confirmed live: tightening a constructor's type in #3738 introduced a
real new ``[arg-type]`` error in ``session.py``, which already carries an
``[arg-type]`` baseline entry from an unrelated pre-existing finding — the
ratchet stayed green. ``(file, line, code)`` would close this, but line
numbers do not survive an unrelated edit shifting them on a moving ``main``
— regenerating the baseline to follow line drift is indistinguishable from
regenerating it to silence a real new failure, which is the one way this
module's own docstring already names as defeating the ratchet. So the
coarser grain is a deliberate trade, not an oversight: it buys stability
across unrelated edits at the cost of blindness to same-code same-file
recurrences, and the latter needs a human (or a future, sharper gate) to
catch, same as any other grandfathered debt in this baseline.

The 76 ``reyn.config`` re-export false positives (#1682's ``_reexport()``,
invisible to mypy's static analysis, documented in #3726's own triage) stay
IN the baseline rather than being carved out via a per-module mypy
``ignore_errors`` override — an exclude-config is itself a new declaration to
maintain, and the point of this script is to draw one line (the baseline)
before adding more.

#3727 (verification-hazards.md §18 "B. Misidentification"): a ``[syntax]``
finding is not "one more red" the way
``[attr-defined]``/``[arg-type]`` are. mypy hits a fatal parse error and
stops the WHOLE invocation ("errors prevented further checking") — every
OTHER file's findings this run are simply unmeasured, not confirmed clean.
Verified directly: injecting a fresh `` # type: `` -prefixed prose comment
(the #3726/#3728 collision shape) into an otherwise-clean file makes THAT
file the only one mypy reports on, full stop. ``main()`` prints a distinct
warning when a new ``[syntax]`` pair appears, because the red's own SHAPE
carries information a bare pair list does not: fix that one before trusting
anything else this run says.

#4576 is that same hazard one layer earlier: not "the run was truncated" but
"the run never happened." With mypy absent from the interpreter's environment,
``python -m mypy`` writes ``No module named mypy`` to stderr and exits 1 —
which :func:`run_mypy` deliberately does not raise on (mypy's own error exit
is the common case), and which :func:`parse_mypy_output` finds no
``[code]`` lines in. Zero measured pairs minus the baseline is zero new pairs,
so the script printed ``OK: 0 findings, all baselined (215 declared)`` and
exited 0 — the ``215 declared`` making it look fully alive, because loading
the baseline HAD succeeded. Measured directly in a ``python -m venv`` with no
mypy installed; found because #4575's local run was green and CI's was not,
and the real ``[call-arg]`` it had been hiding surfaced the moment mypy
actually ran.

The guard is :func:`mypy_is_importable`, checked BEFORE the run: a structural
question (is the module there) rather than a textual one (does the output look
like a real run). Deliberately not a check on mypy's summary wording — that
would make a third party's output format decide whether we trust our own
measurement, and mypy is free to reword it. The exit code cannot serve either:
a missing module and a normal findings-reported run BOTH exit 1 (measured).
NOT covered, disclosed rather than silently half-closed: mypy installed but
crashing mid-run in some shape the ``[syntax]`` guard above doesn't already
name. That shape has never been observed here; this one had.

#5882 (architect ruling — 2026-09-06 owner-machine swap incident, 8 GB RAM):
whole-tree ``mypy tests`` (1,711 files) plus ``mypy src/reyn`` (640 files)
holds a typed AST for the whole population in memory (RSS 1-3 GB, several
minutes), and this repo's own workflow gives every task a FRESH git
worktree — mypy's own ``.mypy_cache`` defaults to the invoking process's
cwd, so every worktree started cold, incremental caching bought nothing
(measured: 27 separate ``.mypy_cache`` directories, 2.8 GB total, scattered
under one coder's worktrees). Two coders running this at once on a
free-12%-RAM machine forced it into swap, ~5x slower, past the 300s
subprocess timeout, retried — 7 times in one night. CI's own ``mypy
(ratchet)`` job makes the same judgment on a separate machine regardless, so
a full local run's only real value was "find out sooner" — not a second,
independent verdict.

Three changes, all from the architect's own ruling (issue #5882):

1. **One shared cache across every worktree of this repo** —
   :func:`default_cache_dir` resolves ``--cache-dir`` from ``git
   rev-parse --git-common-dir`` (OUTSIDE any single worktree's own tree,
   survives a worktree being removed) rather than mypy's own cwd-relative
   default. mypy's cache entries are keyed by source hash, so a cache
   warmed by one worktree's content answering for a DIFFERENT worktree's
   run cannot produce a wrong answer — a stale entry is simply
   recomputed, never trusted blind.

2. **Local default is changed-files mode, not the whole tree** — see
   :func:`changed_python_files` / :func:`main` below: only the ``.py``
   files this branch actually touched (relative to ``origin/main``) are
   passed to mypy, and the baseline comparison is SCOPED to those same
   files. ``--full`` restores the old whole-tree behavior; CI always
   passes it explicitly, and a checkout where ``origin/main`` cannot be
   resolved (no such remote-tracking ref) falls back to it automatically,
   printing why.

   ⚠️ Disclosure (architect's own wording, also in
   ``docs/deep-dives/contributing/pr-workflow.md``): changed-files mode
   only sees NEW findings inside the files this branch touched. A
   signature change that breaks an UNCHANGED caller elsewhere in the tree
   is invisible to it — that shape is exactly what CI's own ``--full``
   run exists to catch. Local changed-files mode is a "find out sooner"
   tool, not a second judgment; CI's ``--full`` run is still the one that
   decides.

3. **A repo-wide lock, so "two runs at once" cannot happen at all** —
   :func:`acquire_lock` takes an ``fcntl.flock`` on ``run.lock`` inside
   the shared cache dir before either mypy subprocess starts. A second
   concurrent invocation waits (printing that it is waiting) unless
   ``--no-wait`` is passed, in which case it exits 2 immediately. This is
   the charter's own "who stops this if it repeats" subject for the
   swap incident above — 1 and 2 alone shrink each individual run, but
   without this a machine could still see two shrunk-but-still-real runs
   overlap on the same night the incident happened.

``dmypy`` (mypy's daemon mode) is deliberately NOT adopted here — the
ruling defers it to "after 1-3, measured": the three changes above may
already remove enough of the cost that a daemon's own complexity (a
long-lived process per repo, its own staleness/restart questions) is not
worth taking on without first seeing whether it is still needed.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "mypy_ratchet_baseline.json"
_TARGET = "src/reyn"
#: #5739: the SEPARATE target for the tests/-scoped None-arg-type gate
#: below — never folded into ``_TARGET``/the baselined ratchet above.
#: Architect's own measurement: `tests/` as a whole carries 3,248+ mypy
#: errors (762 `[arg-type]` alone), most of them structural false
#: positives (CLAUDE.md's own sanctioned test-double/fake pattern, which
#: mypy has no way to know is deliberate) — baselining THAT population
#: would be a second ratchet that only ever grows. This target is used
#: ONLY for the narrow, zero-false-positive shape :func:`none_arg_type_
#: hits_in_tests` looks for, never for a general tests/ mypy sweep.
_TESTS_TARGET = "tests"
#: #5882: the base ref changed-files mode diffs against. A module constant
#: (not a CLI flag) — the ruling's own scope is "local vs CI", not "diff
#: against an arbitrary ref"; a future need for the latter can add a flag
#: without touching this default.
_BASE_REF = "origin/main"
#: #5882: the lock file inside the shared cache dir. Its PARENT directory
#: doubling as both "mypy's own cache" and "this script's own lock" is
#: deliberate, not a namespace collision — both are per-repo, both must
#: survive a worktree's own removal, and a caller who deletes the whole
#: cache dir to force a cold run also correctly drops any stale lock.
_LOCK_FILENAME = "run.lock"

# Matches mypy's normal-mode error line:
#   src/reyn/foo/bar.py:123: error: <message>  [error-code]
# Deliberately excludes `note:` lines (mypy emits explanatory notes attached
# to some errors, e.g. "note: Error code ... not covered by ...") and the
# trailing "Found N errors in M files" summary line — neither carries a
# `[code]` a future run can be diffed against.
_ERROR_LINE = re.compile(r"^(?P<file>[^:]+\.py):\d+: error: .*\[(?P<code>[a-z][a-z0-9-]*)\]\s*$")

# #5739: the one arg-type shape architect ruled has NO syntactic defense —
# a `None` literal passed to a parameter mypy resolves as non-Optional.
# Captures the line number and the exact message text too (unlike
# `_ERROR_LINE` above): this gate has NO baseline (architect: "0 から
# 始まる"), so a caller needs the precise site to fix, not just which
# (file, code) pair recurred — the coarser grain above exists specifically
# to survive line-drift across a BASELINE's lifetime, which does not apply
# to a gate that keeps no baseline at all.
_NONE_ARG_TYPE_LINE = re.compile(
    r"^(?P<file>tests/[^:]+\.py):(?P<lineno>\d+): error: "
    r'(?P<msg>.*incompatible type "None"; expected.*)\[arg-type\]\s*$'
)


def mypy_is_importable() -> bool:
    """Whether ``mypy`` can be imported by the interpreter that will run it.

    The precondition every number this script prints depends on: with mypy
    absent, the measured set is empty for a reason that has nothing to do with
    the code under analysis, and "0 new findings" then means "0 findings were
    looked for" (#4576).
    """
    return importlib.util.find_spec("mypy") is not None


_MYPY_MISSING = (
    "mypy is not importable by {exe} — NOTHING WAS MEASURED. This is not "
    "'0 findings': the baseline may or may not still hold, and this run "
    "cannot tell you which. Install it (`pip install -e \".[dev]\"`) and run "
    "again. Green here without mypy present is the #4576 false green, which "
    "hid a real [call-arg] through a full local pre-PR check."
)


def default_cache_dir(root: Path = _ROOT) -> Path:
    """The shared ``--cache-dir`` for every mypy invocation this script
    makes: ``<git-common-dir>/reyn-mypy-cache`` (#5882).

    ``git rev-parse --git-common-dir`` resolves to the MAIN checkout's
    ``.git`` for any worktree (worktrees share one common-dir by
    construction) and to the ordinary ``.git`` for a non-worktree
    checkout either way — so every worktree of this repo, and the main
    checkout itself, converge on the SAME cache directory, warmed by
    whichever of them ran mypy most recently. This is OUTSIDE any single
    worktree's own working tree, so removing a worktree (the normal way
    a coder's task-scoped checkout is cleaned up) never touches it.

    mypy's own cache entries are keyed by source content hash, not by
    which worktree wrote them — a cache entry warmed while looking at one
    worktree's version of a file is simply a cache MISS (recomputed, not
    trusted) when a different worktree's version of that file differs, so
    sharing the cache across worktrees cannot make mypy report a wrong
    answer, only a slower-than-warm one on the files that actually
    changed between worktrees.

    Raises ``subprocess.CalledProcessError`` if ``root`` is not inside a
    git checkout at all — a caller that reaches this function is already
    inside one (this script itself is), so that failure would mean
    something is badly wrong with the invocation, not an ordinary
    "no ref" case (unlike :func:`changed_python_files`'s OWN, expected,
    "not resolvable" case for ``origin/main``)."""
    proc = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    common_dir = (root / proc.stdout.strip()).resolve()
    return common_dir / "reyn-mypy-cache"


def acquire_lock(cache_dir: Path, *, wait: bool = True, out: "IO[str]" = sys.stderr) -> "IO[Any] | None":
    """Take an exclusive, repo-wide lock before any mypy subprocess starts
    (#5882 — the charter's "who stops this if it repeats" subject for the
    swap incident this module's own docstring describes).

    ``fcntl.flock`` on an open file, not a PID file or a bare "does
    run.lock exist" check: flock is held per OPEN FILE DESCRIPTION (not
    per process), auto-releases if the holder dies without cleanup
    (no stale-lock-from-a-killed-process hazard a PID file would have),
    and — the property this module's own tests rely on — TWO separate
    ``open()`` calls on the same path within a SINGLE process still
    contend with each other, so a test can simulate "another run holds
    the lock" without a real second process.

    Returns the open, locked file handle (the caller holds it for as
    long as the lock should be held, then passes it to
    :func:`release_lock`), or ``None`` if ``wait=False`` and the lock was
    already held.

    ``wait=True`` (the default) tries non-blocking FIRST so it can print
    "waiting" exactly once before falling into a real blocking wait —
    silently blocking with no explanation would look identical to a
    hang to whoever is watching."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / _LOCK_FILENAME
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        if not wait:
            handle.close()
            return None
        print(f"another mypy run is holding {lock_path}; waiting", file=out)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # blocks until released
        return handle


def release_lock(handle: "IO[Any] | None") -> None:
    """Release a lock :func:`acquire_lock` returned. A no-op on ``None``
    (the ``--no-wait`` "did not acquire" case) so a caller's ``finally``
    never needs its own conditional."""
    if handle is None:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def changed_python_files(root: Path = _ROOT, base_ref: str = _BASE_REF) -> "list[str] | None":
    """The ``.py`` files this checkout's ``HEAD`` changed relative to
    ``base_ref``, plus untracked ``.py`` files — the population
    changed-files mode checks (#5882).

    Returns ``None`` when ``base_ref`` cannot be resolved (no such
    remote-tracking ref — a shallow clone, a fork without ``origin/main``
    configured, or CI's own non-PR contexts) so the caller can fall back
    to ``--full`` instead of silently checking nothing.

    Committed changes only (``git diff --name-only --diff-filter=ACMR
    <base_ref>...HEAD``, three-dot = against the MERGE-BASE, so a branch
    that has diverged from a moving ``main`` is diffed against where it
    branched, not against ``main``'s current tip) plus untracked new
    files — deliberately NOT staged-or-unstaged edits to an already
    TRACKED file. Architect's own ruling scope; a caller mid-edit with
    uncommitted changes to a tracked file should commit (or use
    ``--full``) before relying on this for anything but a quick look —
    disclosed here and in ``pr-workflow.md``, not silently assumed.

    ``--diff-filter=ACMR`` (Added/Copied/Modified/Renamed) excludes
    Deleted — nothing to type-check in a file that no longer exists."""
    resolvable = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", base_ref],
        cwd=root, capture_output=True, text=True,
    )
    if resolvable.returncode != 0:
        return None
    diff = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return sorted({f for f in (*diff, *untracked) if f.endswith(".py")})


def split_src_and_tests(files: "list[str]") -> "tuple[list[str], list[str]]":
    """Partition ``files`` (repo-relative paths) into the (src, tests)
    populations the two gates below each cover. A file under neither
    ``src/reyn/`` nor ``tests/`` (e.g. ``scripts/*.py``) is checked by
    NEITHER gate — the same scope the pre-#5882 whole-tree targets
    (:data:`_TARGET`, :data:`_TESTS_TARGET`) already had; changed-files
    mode narrows WHICH files within that scope are checked, not the
    scope itself."""
    src = [f for f in files if f.startswith("src/reyn/")]
    tests = [f for f in files if f.startswith("tests/")]
    return src, tests


def run_mypy(
    targets: "list[str]",
    *,
    cache_dir: Path,
    root: Path = _ROOT,
    runner: "Callable[..., subprocess.CompletedProcess[str]]" = subprocess.run,
) -> str:
    """Run mypy against ``targets`` (either ``[_TARGET]`` for a whole-tree
    run, or a specific list of changed files) and return its combined
    stdout+stderr.

    ``runner`` is injectable (#5882 acceptance ②) so a test can record the
    exact argv this function builds — including ``--cache-dir`` and the
    target list — without a real mypy install or a real subprocess.

    Never raises on a non-zero exit — mypy exits 1 whenever it reports any
    error, which is the expected, common case this script exists to handle,
    not a failure of the subprocess call itself.
    """
    proc = runner(
        [sys.executable, "-m", "mypy", "--cache-dir", str(cache_dir), *targets],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.stdout + proc.stderr


def parse_mypy_output(text: str) -> "set[tuple[str, str]]":
    """Extract the ``{(file, error-code)}`` set mypy's output reports.

    Multiple error lines in the same file for the same code (mypy repeats a
    `[attr-defined]` finding once per occurrence) collapse to one pair — the
    ratchet is keyed on "does this file still carry this CLASS of finding",
    not on exact line counts (a line shifting because of an unrelated edit
    above it must never itself flip the gate red)."""
    pairs: set[tuple[str, str]] = set()
    for line in text.splitlines():
        m = _ERROR_LINE.match(line)
        if m:
            pairs.add((m.group("file"), m.group("code")))
    return pairs


def run_mypy_tests_none_arg_type(
    targets: "list[str]",
    *,
    cache_dir: Path,
    root: Path = _ROOT,
    runner: "Callable[..., subprocess.CompletedProcess[str]]" = subprocess.run,
) -> str:
    """Run mypy against ``targets`` (either ``[_TESTS_TARGET]`` for a
    whole-tree run, or a specific list of changed test files) and return
    combined stdout+stderr (#5739, targets/runner added #5882).

    ``MYPYPATH=src`` — unlike the ``src/reyn`` target above, ``tests/``
    itself is not on mypy's default search path, so an ``import reyn.foo``
    inside a test file cannot resolve without pointing mypy at the real
    checkout's ``src/`` (architect's own measurement command:
    ``MYPYPATH=src python -m mypy tests``). A caller of this script from a
    *different* checkout, or one whose environment already resolves
    ``reyn`` via an editable/site-packages install, is unaffected either
    way — this only ADDS a search path, never removes one.

    A SEPARATE subprocess call from :func:`run_mypy` (different target,
    different env) — never folds the two together, so `--write-baseline`
    (which only ever touches the ``src/reyn`` baseline) cannot accidentally
    absorb a ``tests/`` finding into a population it was never meant to
    cover.
    """
    env = {**os.environ, "MYPYPATH": str(root / "src")}
    proc = runner(
        [sys.executable, "-m", "mypy", "--cache-dir", str(cache_dir), *targets],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    return proc.stdout + proc.stderr


def none_arg_type_hits_in_tests(
    text: str, *, scope: "set[str] | None" = None,
) -> "set[tuple[str, int, str]]":
    """Extract every (file, line, message) triple matching the #5739 shape:
    a `tests/` call site passing a `None` literal to an argument mypy
    resolves as non-Optional.

    NO baseline gates this — every hit is unconditionally new (architect:
    "gate は検出器ではなく修理義務" — a detector reports; a repair
    obligation has nothing left to grandfather). Keyed on the EXACT site
    (file, line, message), not the coarser (file, code) grain
    :func:`parse_mypy_output` uses above — see :data:`_NONE_ARG_TYPE_LINE`'s
    own comment for why the coarser grain does not apply to a baseline-free
    gate.

    ``scope`` (#5882): when given (changed-files mode), a hit whose file is
    outside ``scope`` is dropped — mypy following an import from a changed
    test file into an UNCHANGED one could otherwise surface a pre-existing
    hit that changed-files mode has no business reporting on this run
    (``--full`` still catches it)."""
    hits: set[tuple[str, int, str]] = set()
    for line in text.splitlines():
        m = _NONE_ARG_TYPE_LINE.match(line)
        if m:
            hits.add((m.group("file"), int(m.group("lineno")), m.group("msg").strip()))
    if scope is not None:
        hits = {h for h in hits if h[0] in scope}
    return hits


def load_baseline(path: Path = _BASELINE_PATH) -> "set[tuple[str, str]]":
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(entry["file"], entry["code"]) for entry in data}


def write_baseline(pairs: "set[tuple[str, str]]", path: Path = _BASELINE_PATH) -> None:
    data = [{"file": f, "code": c} for f, c in sorted(pairs)]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def new_findings(
    measured: "set[tuple[str, str]]",
    baseline: "set[tuple[str, str]]",
    *,
    scope: "set[str] | None" = None,
) -> "set[tuple[str, str]]":
    """The ratchet check itself: any measured pair the baseline does not
    already declare is new — a pair leaving the measured set (a fix) is not
    reported here at all, by design (see module docstring).

    ``scope`` (#5882): when given (changed-files mode), only pairs whose
    file is IN ``scope`` are compared at all — an off-baseline pair in an
    UNCHANGED file (surfaced only because mypy followed an import into it)
    must stay invisible to changed-files mode; it is exactly the shape
    ``--full`` exists to still catch."""
    if scope is not None:
        measured = {p for p in measured if p[0] in scope}
    return measured - baseline


def syntax_pairs_in(pairs: "set[tuple[str, str]]") -> "set[tuple[str, str]]":
    """The `[syntax]` subset of ``pairs`` — mypy's signal that it hit a fatal
    parse error and stopped ("errors prevented further checking"), the SAME
    shape #3726/#3728 found at `config/root.py:147`: trusting a truncated
    run as if it were a complete one is a misidentification of what was
    actually measured (docs/deep-dives/contributing/verification-hazards.md
    §18 "B. Misidentification").

    Takes ``measured``, not ``new`` — a `[syntax]` pair that happens to
    already be baselined is NOT "known debt" the way every other code is: a
    baselined `[attr-defined]` means "we've seen this file's finding before
    and haven't fixed it," but a baselined `[syntax]` would mean "the last
    time this ran, mypy checked exactly one file and called it OK," and every
    run after would silently repeat that with no new pair to flag (#3727
    review). `main()` treats ANY `[syntax]` pair in `measured` as fatal,
    baselined or not, and refuses to bake one into the baseline at all via
    `--write-baseline` — the one operation the module docstring already names
    as "the one way to defeat the ratchet" would otherwise defeat THIS
    specific check permanently, silently, in one command.

    Deliberately NOT scoped to changed-files mode's own file set (#5882): a
    truncated run means "trust nothing this run says" regardless of which
    files were the intended target — the whole point of #3727's own finding
    is that a `[syntax]` abort makes every OTHER file's silence
    UNINFORMATIVE, not confirmed clean, so narrowing this check to "only
    care if the syntax error is in a file I meant to check" would reinstate
    exactly the misidentification #3727 closed."""
    return {p for p in pairs if p[1] == "syntax"}


_SYNTAX_ABORT_WARNING = (
    "[syntax] above is not \"one more red\": a fatal parse error stops "
    "mypy's ENTIRE run, so no other file was actually checked this time — "
    "every OTHER pair this run's output doesn't mention is UNMEASURED, "
    "not confirmed clean. Fix the [syntax] finding first; nothing else "
    "this run says can be trusted until it's gone (verification-hazards.md "
    "§18 \"B. Misidentification\")."
)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from a fresh mypy run instead of checking "
            "against it. Use ONLY after actually fixing/triaging findings — "
            "regenerating to silence a new failure defeats the ratchet (see "
            "module docstring). Always runs against the WHOLE tree (--full is "
            "implied) — a baseline scoped to changed files would silently "
            "drop every pair outside them the next time someone runs this."
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "check the whole src/reyn + tests trees (#5882) — CI's own "
            "mode, always passed explicitly there. Local runs default to "
            "changed-files mode instead (only .py files this branch "
            "touched relative to origin/main); a checkout where "
            "origin/main cannot be resolved falls back to this "
            "automatically."
        ),
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "if another mypy_ratchet.py run already holds the repo-wide "
            "lock, exit 2 immediately instead of waiting for it (#5882)."
        ),
    )
    args = parser.parse_args(argv)

    # #4576: before anything else — including --write-baseline, which would
    # otherwise overwrite the baseline with the empty measurement of a run
    # that never happened, silently discarding every declared pair. Guards
    # BOTH mypy invocations below (the src/reyn ratchet and the #5739
    # tests/ None-arg-type gate) — the same import either both runs use.
    if not mypy_is_importable():
        print(_MYPY_MISSING.format(exe=sys.executable), file=sys.stderr)
        return 1

    # #5882: --write-baseline always operates on the whole tree — a
    # baseline is a claim about the WHOLE population's current findings,
    # and writing one scoped to a diff would silently drop every pair
    # outside it the next time someone regenerates.
    full = args.full or args.write_baseline

    src_scope: "set[str] | None" = None
    tests_scope: "set[str] | None" = None
    if full:
        src_targets = [_TARGET]
        tests_targets = [_TESTS_TARGET]
    else:
        changed = changed_python_files()
        if changed is None:
            print(
                f"{_BASE_REF} is not resolvable in this checkout — falling "
                "back to --full (changed-files mode needs a base ref to "
                "diff against).",
                file=sys.stderr,
            )
            full = True
            src_targets = [_TARGET]
            tests_targets = [_TESTS_TARGET]
        else:
            src_files, tests_files = split_src_and_tests(changed)
            src_scope, tests_scope = set(src_files), set(tests_files)
            src_targets, tests_targets = src_files, tests_files
            if not src_targets and not tests_targets:
                print(
                    "mypy ratchet: no changed .py file under src/reyn/ or "
                    "tests/ relative to origin/main — nothing to check "
                    "locally. This is a \"find out sooner\" tool, not the "
                    "judgment (see module docstring) — CI's own --full run "
                    "is still the gate."
                )
                return 0

    cache_dir = default_cache_dir()
    lock_handle = acquire_lock(cache_dir, wait=not args.no_wait)
    if lock_handle is None:
        print(
            f"another mypy_ratchet.py run is holding the lock at "
            f"{cache_dir / _LOCK_FILENAME}; refusing to wait (--no-wait).",
            file=sys.stderr,
        )
        return 2

    try:
        # #5739: the tests/-scoped, baseline-free gate — checked in EVERY
        # mode (including --write-baseline, which only ever regenerates the
        # OTHER gate's baseline and has no bearing on this one). Any hit is
        # unconditionally a failure; there is nothing here to grandfather.
        # Skipped entirely (no subprocess) when changed-files mode found no
        # changed tests/ file — the whole point of #5882 is not spending a
        # multi-minute run when there is nothing in scope to check.
        none_arg_hits: "set[tuple[str, int, str]]" = set()
        if tests_targets:
            none_arg_hits = none_arg_type_hits_in_tests(
                run_mypy_tests_none_arg_type(tests_targets, cache_dir=cache_dir),
                scope=tests_scope,
            )

        measured: "set[tuple[str, str]]" = set()
        if src_targets:
            output = run_mypy(src_targets, cache_dir=cache_dir)
            measured = parse_mypy_output(output)
        syntax_pairs = syntax_pairs_in(measured)

        def _report_none_arg_hits() -> None:
            print(
                f"\n#5739 gate FAILED: {len(none_arg_hits)} tests/ site(s) pass a "
                f"None literal to a non-Optional argument (no baseline — fix "
                f"each site; see the finding's own message for whether the TEST "
                f"or the production signature is wrong):",
                file=sys.stderr,
            )
            for file, lineno, msg in sorted(none_arg_hits):
                print(f"  {file}:{lineno}: {msg}", file=sys.stderr)

        if args.write_baseline:
            if syntax_pairs:
                print(
                    "REFUSING to write baseline: this measurement contains a "
                    "[syntax] finding, which means mypy aborted before checking "
                    "most of the tree. Baselining it would bake in a "
                    "permanently-degraded run that reports \"OK\" forever while "
                    "only ever checking one file. Fix the [syntax] finding(s) "
                    "first, then regenerate.\n",
                    file=sys.stderr,
                )
                for file, code in sorted(syntax_pairs):
                    print(f"  {file}  [{code}]", file=sys.stderr)
                return 1
            write_baseline(measured)
            print(f"Wrote {len(measured)} (file, code) pairs to {_BASELINE_PATH}")
            if none_arg_hits:
                # #5739 has no baseline to write — surfaced here too so
                # --write-baseline never reads as "everything is clean".
                _report_none_arg_hits()
                return 1
            return 0

        baseline = load_baseline()
        new = new_findings(measured, baseline, scope=src_scope)

        if not new and not syntax_pairs and not none_arg_hits:
            mode = "--full" if full else "changed-files"
            print(
                f"mypy ratchet OK ({mode}): {len(measured)} findings, all "
                f"baselined ({len(baseline)} declared)."
            )
            return 0

        print("mypy ratchet FAILED:\n", file=sys.stderr)
        if new:
            print(
                f"{len(new)} new (file, error-code) pair(s) not in the baseline "
                f"({_BASELINE_PATH.relative_to(_ROOT)}):",
                file=sys.stderr,
            )
            for file, code in sorted(new):
                print(f"  {file}  [{code}]", file=sys.stderr)
        if syntax_pairs:
            # Fires even when every syntax_pairs entry is already baselined (so
            # absent from `new`) — a baselined [syntax] is never "known debt",
            # see syntax_pairs_in()'s own docstring.
            print(f"\n{_SYNTAX_ABORT_WARNING}", file=sys.stderr)
            for file, code in sorted(syntax_pairs):
                note = "" if (file, code) in new else "  (already \"baselined\" — still fatal, see above)"
                print(f"  {file}  [{code}]{note}", file=sys.stderr)
        if none_arg_hits:
            _report_none_arg_hits()
        print(
            "\nEither fix the new finding(s), or if this is a deliberate, understood "
            "addition, add the (file, code) pair to the baseline explicitly — do "
            "NOT regenerate the whole baseline with --write-baseline to silence this. "
            "(The #5739 gate above has no baseline at all — fix those sites directly.)",
            file=sys.stderr,
        )
        return 1
    finally:
        release_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
