"""Tier 1: scripts/mypy_ratchet.py's parse/diff contract.

Same skeleton as `tests/interfaces/test_3595_s4_slash_handler_seam.py`'s `_SESSION_RESIDUE`
ratchet — a committed baseline set only ever shrinks; a measured entry not in
it is new and must be surfaced, an entry that silently disappears from the
measured set (a fix) is not itself reported. Here the measured set is
`(file, mypy-error-code)` pairs rather than private-member accesses, and the
"walk" is running mypy itself rather than an AST pass, but the ratchet logic
(`new_findings`) is the same shape: `measured - baseline`, nothing more.

Public surface only: `parse_mypy_output` / `new_findings` / `load_baseline` /
`write_baseline` are called directly against real strings/files (no mocks —
there is nothing to fake here, the whole point is these are pure functions
over text/sets).

#5882 additions (cache-dir sharing / changed-files mode / the repo-wide
lock): every `main()`-driving test below now forces `--full` and points
`default_cache_dir` at a throwaway `tmp_path` — `main([])`'s new DEFAULT is
changed-files mode, which would otherwise run a real `git diff` against
THIS checkout's actual working-tree state and make these tests' own
target/scope non-deterministic; `--full` restores the old, deterministic
whole-tree semantics these tests were written against. Pointing
`default_cache_dir` at `tmp_path` keeps every test from touching (or
contending on) the real, shared `<git-common-dir>/reyn-mypy-cache` a
genuine concurrent mypy_ratchet.py run elsewhere on the machine might be
holding.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "mypy_ratchet.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mypy_ratchet_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeCompleted:
    """A real, minimal stand-in for `subprocess.CompletedProcess` — not a
    mock, just the two attributes `run_mypy`/`run_mypy_tests_none_arg_type`
    read off whatever `runner` returns."""

    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


# ── parse_mypy_output ───────────────────────────────────────────────────────


def test_parses_error_lines_into_file_code_pairs() -> None:
    """Tier 1: an ordinary mypy error line becomes one (file, code) pair."""
    module = _load()
    text = (
        'src/reyn/foo.py:12: error: Name "X" is not defined  [name-defined]\n'
        'src/reyn/bar.py:34: error: Argument 1 has incompatible type  [arg-type]\n'
    )
    assert module.parse_mypy_output(text) == {
        ("src/reyn/foo.py", "name-defined"),
        ("src/reyn/bar.py", "arg-type"),
    }


def test_repeated_lines_in_the_same_file_and_code_collapse_to_one_pair() -> None:
    """Tier 1: the ratchet is keyed on (file, code), not exact line/occurrence
    count — a line shifting from an unrelated edit above it must never flip
    the gate by itself."""
    module = _load()
    text = (
        'src/reyn/foo.py:12: error: msg one  [attr-defined]\n'
        'src/reyn/foo.py:99: error: msg two  [attr-defined]\n'
    )
    assert module.parse_mypy_output(text) == {("src/reyn/foo.py", "attr-defined")}


def test_note_lines_and_the_summary_line_are_not_parsed_as_findings() -> None:
    """Tier 1: FALSIFY — a `note:` line or the trailing `Found N errors...`
    summary carry no `[code]` a future run can diff against; parsing them
    would corrupt the baseline with entries that can never be matched again."""
    module = _load()
    text = (
        'src/reyn/foo.py:12: error: msg  [assignment]\n'
        'src/reyn/foo.py:12: note: Error code "arg-type" not covered by "type: ignore[assignment]" comment\n'
        "Found 1 error in 1 file (checked 5 source files)\n"
    )
    assert module.parse_mypy_output(text) == {("src/reyn/foo.py", "assignment")}


# ── new_findings (the ratchet itself) ───────────────────────────────────────


def test_a_measured_pair_absent_from_baseline_is_new() -> None:
    """Tier 1: FALSIFY — a pair the baseline never declared must surface as new."""
    module = _load()
    measured = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}
    baseline = {("src/reyn/foo.py", "attr-defined")}
    assert module.new_findings(measured, baseline) == {("src/reyn/bar.py", "arg-type")}


def test_a_baselined_pair_that_disappears_from_measured_is_not_reported() -> None:
    """Tier 1: a fix (a baselined pair no longer measured) never appears in
    `new_findings` — nothing has to be edited to let a fix 'count', per the
    module's own design (mirrors #3595's ratchet: only growth is gated)."""
    module = _load()
    measured: set[tuple[str, str]] = set()
    baseline = {("src/reyn/foo.py", "attr-defined")}
    assert module.new_findings(measured, baseline) == set()


def test_measured_set_equal_to_baseline_is_clean() -> None:
    """Tier 1: nothing new against an identical measured/baseline set."""
    module = _load()
    pairs = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}
    assert module.new_findings(pairs, pairs) == set()


def test_changed_files_mode_hides_an_off_baseline_pair_in_an_unchanged_file() -> None:
    """Tier 1: #5882 accept ③ — positive control. An off-baseline pair in a
    file OUTSIDE changed-files mode's `scope` stays invisible (mypy
    following an import into an unchanged file must not resurrect a
    pre-existing finding on THIS run) — the SAME pair surfaces the moment
    `scope` is removed (`--full`), proving the hiding is `scope`-driven,
    not a bug that would hide it everywhere."""
    module = _load()
    measured = {
        ("src/reyn/changed.py", "arg-type"),
        ("src/reyn/unchanged.py", "attr-defined"),
    }
    baseline = {("src/reyn/changed.py", "arg-type")}  # unchanged.py's pair is NOT baselined

    scoped = module.new_findings(measured, baseline, scope={"src/reyn/changed.py"})
    assert scoped == set(), (
        "an off-baseline pair OUTSIDE changed-files mode's scope must stay green"
    )

    full = module.new_findings(measured, baseline, scope=None)
    assert full == {("src/reyn/unchanged.py", "attr-defined")}, (
        "the SAME off-baseline pair must surface once scope is removed (--full)"
    )


# ── syntax_pairs_in (#3727, verification-hazards.md §18 "B. Misidentification") ──


def test_a_syntax_pair_is_extracted_from_measured() -> None:
    """Tier 1: FALSIFY — a `[syntax]` pair in `measured` is flagged, since
    mypy aborts its whole run on one and every other file's findings go
    unmeasured that run."""
    module = _load()
    measured = {("src/reyn/foo.py", "syntax"), ("src/reyn/bar.py", "arg-type")}
    assert module.syntax_pairs_in(measured) == {("src/reyn/foo.py", "syntax")}


def test_ordinary_findings_yield_no_syntax_pairs() -> None:
    """Tier 1: an ordinary [attr-defined]/[arg-type] red is a normal
    finding, not the "nothing else this run says can be trusted" shape."""
    module = _load()
    measured = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}
    assert module.syntax_pairs_in(measured) == set()


def test_empty_measured_yields_no_syntax_pairs() -> None:
    """Tier 1: nothing measured means nothing to warn about."""
    module = _load()
    assert module.syntax_pairs_in(set()) == set()


def test_a_baselined_syntax_pair_still_counts_because_it_is_taken_from_measured() -> None:
    """Tier 1: FALSIFY the exact gap lead-coder's review found — a
    `[syntax]` pair that is ALREADY in the baseline is not "known debt" the
    way every other code is (a baselined [syntax] means a past run checked
    exactly one file and called it OK forever after). `syntax_pairs_in`
    takes `measured` directly, never `new`, so this is caught regardless of
    baseline membership."""
    module = _load()
    measured = {("src/reyn/foo.py", "syntax")}
    baseline = {("src/reyn/foo.py", "syntax")}  # already declared
    assert module.new_findings(measured, baseline) == set()  # invisible to `new`
    assert module.syntax_pairs_in(measured) == {("src/reyn/foo.py", "syntax")}  # NOT invisible here


# ── load_baseline / write_baseline round-trip ───────────────────────────────


def test_write_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 1: what write_baseline puts on disk, load_baseline reads back
    unchanged — the maintenance path (`--write-baseline`) round-trips."""
    module = _load()
    path = tmp_path / "baseline.json"
    pairs = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}

    module.write_baseline(pairs, path)
    loaded = module.load_baseline(path)

    assert loaded == pairs


def test_written_baseline_is_sorted_for_stable_diffs(tmp_path: Path) -> None:
    """Tier 1: sorted output keeps the committed file's diffs reviewable —
    an unsorted dump would make every regeneration a full-file reorder."""
    module = _load()
    path = tmp_path / "baseline.json"
    pairs = {("src/reyn/zzz.py", "attr-defined"), ("src/reyn/aaa.py", "arg-type")}

    module.write_baseline(pairs, path)
    data = json.loads(path.read_text(encoding="utf-8"))

    files = [entry["file"] for entry in data]
    assert files == sorted(files)


# ── the real repo, end to end ────────────────────────────────────────────────


def test_the_committed_baseline_is_reachable_through_the_registered_target() -> None:
    """Tier 1: `load_baseline()`'s own default (no path argument, the same
    default `main()` uses) resolves to the real committed baseline — a path
    typo here would silently no-op the CI job (0 findings, 0 baseline,
    trivially "clean") rather than raising."""
    module = _load()
    baseline = module.load_baseline()
    assert len(baseline) > 0


# ── #5882: split_src_and_tests ──────────────────────────────────────────────


def test_split_src_and_tests_partitions_by_top_level_directory() -> None:
    """Tier 1: a changed-files population splits into exactly the two
    populations the src ratchet / #5739 tests gate each cover; a file under
    neither (e.g. scripts/) is in NEITHER output list — same scope the old
    whole-tree targets already had."""
    module = _load()
    src, tests = module.split_src_and_tests(
        ["src/reyn/foo.py", "tests/bar.py", "scripts/baz.py", "docs/readme.md"]
    )
    assert src == ["src/reyn/foo.py"]
    assert tests == ["tests/bar.py"]


# ── #5882: default_cache_dir — shared across worktrees ──────────────────────


def test_cache_dir_resolves_to_the_git_common_dir_not_the_worktree(tmp_path: Path) -> None:
    """Tier 1: #5882 accept ① — from a REAL git worktree, `default_cache_dir`
    resolves under the MAIN checkout's `.git` (shared across every
    worktree of this repo), never the worktree's own directory. strip: the
    naive cwd-relative default this fix replaces would resolve to a path
    INSIDE the worktree instead — verified directly by asserting the
    resolved path is NOT under the worktree and IS under the main repo."""
    module = _load()
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _run_git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=main_repo, check=True, capture_output=True, text=True,
    )
    _run_git("init", "-q")
    _run_git("config", "user.email", "t@example.com")
    _run_git("config", "user.name", "t")
    (main_repo / "f.txt").write_text("x")
    _run_git("add", ".")
    _run_git("commit", "-q", "-m", "init")
    _run_git("branch", "wt-branch")

    worktree = tmp_path / "worktree"
    _run_git("worktree", "add", str(worktree), "wt-branch")

    resolved = module.default_cache_dir(root=worktree)

    assert resolved.name == "reyn-mypy-cache"
    assert not str(resolved).startswith(str(worktree)), (
        "cache dir resolved INSIDE the worktree — the shared-across-worktrees "
        "fix regressed to a naive per-worktree default"
    )
    assert str(resolved).startswith(str(main_repo)), (
        "cache dir did not resolve under the MAIN checkout — worktrees no "
        "longer share a warm cache"
    )


# ── #5882: run_mypy / run_mypy_tests_none_arg_type — injectable argv ────────


def test_changed_files_mode_passes_only_the_changed_files_to_mypy() -> None:
    """Tier 1: #5882 accept ② — the exact argv `run_mypy` builds for a
    changed-files call is just those files (plus `--cache-dir`); a
    `--full`-shaped call (`["src/reyn"]`) still gets the WHOLE target.
    `runner` is injected so this needs no real mypy install or subprocess."""
    module = _load()
    recorded: list[list[str]] = []

    def _runner(argv: list[str], **kwargs: object) -> _FakeCompleted:
        recorded.append(argv)
        return _FakeCompleted()

    module.run_mypy(["src/reyn/foo.py"], cache_dir=Path("/tmp/cache"), runner=_runner)
    assert recorded[-1][-1] == "src/reyn/foo.py"
    assert "--cache-dir" in recorded[-1] and "/tmp/cache" in recorded[-1]

    module.run_mypy(["src/reyn"], cache_dir=Path("/tmp/cache"), runner=_runner)
    assert recorded[-1][-1] == "src/reyn"


def test_changed_files_mode_passes_only_the_changed_test_files_to_mypy() -> None:
    """Tier 1: same as above, for the #5739 tests/-scoped invocation."""
    module = _load()
    recorded: list[list[str]] = []

    def _runner(argv: list[str], **kwargs: object) -> _FakeCompleted:
        recorded.append(argv)
        return _FakeCompleted()

    module.run_mypy_tests_none_arg_type(
        ["tests/runtime/test_foo.py"], cache_dir=Path("/tmp/cache"), runner=_runner,
    )
    assert recorded[-1][-1] == "tests/runtime/test_foo.py"

    module.run_mypy_tests_none_arg_type(["tests"], cache_dir=Path("/tmp/cache"), runner=_runner)
    assert recorded[-1][-1] == "tests"


# ── #5882: acquire_lock / release_lock — repo-wide, single-run ──────────────


def test_no_wait_returns_none_immediately_when_the_lock_is_already_held(tmp_path: Path) -> None:
    """Tier 1: #5882 accept ④ — a SECOND, independent open-file-description
    on the same lock path contends via `flock` even within one process
    (`acquire_lock`'s own docstring on why this is testable without a real
    second process). `wait=False` must fail IMMEDIATELY — no sleep, no
    duration — while the first holder has it, and succeed the moment it is
    released."""
    module = _load()
    cache_dir = tmp_path / "cache"

    holder = module.acquire_lock(cache_dir, wait=True)
    assert holder is not None

    second = module.acquire_lock(cache_dir, wait=False)
    assert second is None, (
        "a second, non-waiting acquire must fail immediately while the first is held"
    )

    module.release_lock(holder)

    third = module.acquire_lock(cache_dir, wait=False)
    assert third is not None, "once released, a non-waiting acquire must succeed"
    module.release_lock(third)


def test_main_no_wait_exits_2_when_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 1: #5882 accept ④, wired through `main()` — `--no-wait` against
    an already-held lock is exit code 2, distinct from the ratchet's own
    0/1 verdicts, and never reaches either mypy invocation."""
    module = _load()
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(module, "default_cache_dir", lambda: cache_dir)
    calls: list[str] = []
    monkeypatch.setattr(module, "run_mypy", lambda *a, **kw: calls.append("src") or "")
    monkeypatch.setattr(
        module, "run_mypy_tests_none_arg_type", lambda *a, **kw: calls.append("tests") or "",
    )

    holder = module.acquire_lock(cache_dir, wait=True)
    try:
        rc = module.main(["--full", "--no-wait"])
    finally:
        module.release_lock(holder)

    assert rc == 2
    assert calls == [], "a lock refusal must never reach either mypy invocation"


# ── #5882: main() — changed-files mode selection ────────────────────────────


def test_main_changed_files_mode_skips_mypy_entirely_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: #5882 — with zero changed `.py` files, `main()` spends no
    subprocess call on either gate at all (the whole point of the fix), and
    never touches the cache dir / lock either."""
    module = _load()
    monkeypatch.setattr(module, "changed_python_files", lambda: [])
    calls: list[str] = []
    monkeypatch.setattr(module, "run_mypy", lambda *a, **kw: calls.append("src") or "")
    monkeypatch.setattr(
        module, "run_mypy_tests_none_arg_type", lambda *a, **kw: calls.append("tests") or "",
    )
    monkeypatch.setattr(
        module, "default_cache_dir",
        lambda: (_ for _ in ()).throw(
            AssertionError("cache dir must not be resolved when there is nothing to check")
        ),
    )

    rc = module.main([])

    assert rc == 0
    assert calls == []


def test_main_falls_back_to_full_when_origin_main_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 1: #5882 — a checkout where `origin/main` cannot be resolved
    (`changed_python_files` returns ``None``) falls back to the WHOLE
    `src/reyn` / `tests` targets, same as an explicit `--full`."""
    module = _load()
    monkeypatch.setattr(module, "changed_python_files", lambda: None)
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(module, "load_baseline", lambda: set())
    recorded_src_targets: list[list[str]] = []
    recorded_tests_targets: list[list[str]] = []
    monkeypatch.setattr(
        module, "run_mypy",
        lambda targets, **kw: recorded_src_targets.append(list(targets)) or "",
    )
    monkeypatch.setattr(
        module, "run_mypy_tests_none_arg_type",
        lambda targets, **kw: recorded_tests_targets.append(list(targets)) or "",
    )

    rc = module.main([])

    assert rc == 0
    assert recorded_src_targets == [["src/reyn"]]
    assert recorded_tests_targets == [["tests"]]


# ── main() wiring (#3727 review: the bug lived in main(), not in a pure fn) ──


def test_main_fails_even_when_the_only_measured_pair_is_a_baselined_syntax_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Tier 1: FALSIFY the exact gap lead-coder's review found — the ORIGINAL
    wiring checked `new_findings()` only, so a `[syntax]` pair already in the
    baseline made `main()` print "mypy ratchet OK" every run while mypy had
    actually checked exactly one file. `load_baseline`/`run_mypy` are
    monkeypatched directly (not the `_BASELINE_PATH` constant, whose value is
    already bound into their default-argument at module-def time) so the
    test drives `main()`'s real control flow without touching disk."""
    module = _load()
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(module, "load_baseline", lambda: {("src/reyn/foo.py", "syntax")})
    monkeypatch.setattr(
        module, "run_mypy",
        lambda *a, **kw: "src/reyn/foo.py:1: error: Invalid syntax  [syntax]\n"
                "Found 1 error in 1 file (errors prevented further checking)\n",
    )
    monkeypatch.setattr(module, "run_mypy_tests_none_arg_type", lambda *a, **kw: "")

    rc = module.main(["--full"])

    assert rc == 1
    assert "not confirmed clean" in capsys.readouterr().err


def test_main_ok_when_measured_matches_baseline_with_no_syntax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 1: the ordinary green path is unaffected by the syntax check —
    only actually-present [syntax] pairs change the verdict."""
    module = _load()
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(module, "load_baseline", lambda: {("src/reyn/foo.py", "attr-defined")})
    monkeypatch.setattr(
        module, "run_mypy",
        lambda *a, **kw: "src/reyn/foo.py:1: error: msg  [attr-defined]\n",
    )
    monkeypatch.setattr(module, "run_mypy_tests_none_arg_type", lambda *a, **kw: "")

    assert module.main(["--full"]) == 0


def test_main_write_baseline_refuses_when_a_syntax_pair_is_measured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Tier 1: FALSIFY — `--write-baseline` is the module docstring's own
    named "one way to defeat the ratchet"; baking in a `[syntax]` pair would
    make that operation permanently silence the abort-detection this PR
    adds. `main()` must refuse to write at all — `write_baseline` must never
    even be called."""
    module = _load()
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    calls: list[object] = []
    monkeypatch.setattr(module, "write_baseline", lambda pairs, path=None: calls.append(pairs))
    monkeypatch.setattr(
        module, "run_mypy",
        lambda *a, **kw: "src/reyn/foo.py:1: error: Invalid syntax  [syntax]\n"
                "Found 1 error in 1 file (errors prevented further checking)\n",
    )
    monkeypatch.setattr(module, "run_mypy_tests_none_arg_type", lambda *a, **kw: "")

    rc = module.main(["--write-baseline"])

    assert rc == 1
    assert "REFUSING" in capsys.readouterr().err
    assert calls == []


# ── #4576: mypy_is_importable() must gate the WHOLE script ─────────────────
# (architect's #5739 ruling named this "この gate の最大の失敗様式" — no
# existing test in this file drove it through main() before now; #5739
# adds a second mypy invocation, so the guard's reach matters twice over.)


def test_main_refuses_when_mypy_is_not_importable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 1: FALSIFY the #4576 shape — with mypy absent, `main()` must
    fail LOUDLY (not print "0 findings" and exit 0) and must never even
    reach either mypy invocation — nor the changed-files/cache-dir/lock
    machinery below the guard."""
    module = _load()
    monkeypatch.setattr(module, "mypy_is_importable", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(module, "run_mypy", lambda *a, **kw: calls.append("src") or "")
    monkeypatch.setattr(
        module, "run_mypy_tests_none_arg_type", lambda *a, **kw: calls.append("tests") or "",
    )

    rc = module.main([])

    assert rc == 1
    assert "NOTHING WAS MEASURED" in capsys.readouterr().err
    assert calls == [], "the guard must fire BEFORE either mypy invocation runs"


def test_main_write_baseline_also_refuses_when_mypy_is_not_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: the #4576 guard covers --write-baseline too — regenerating
    the baseline from a run that never happened would silently discard
    every declared pair (module's own docstring)."""
    module = _load()
    monkeypatch.setattr(module, "mypy_is_importable", lambda: False)
    calls: list[object] = []
    monkeypatch.setattr(module, "write_baseline", lambda pairs, path=None: calls.append(pairs))

    rc = module.main(["--write-baseline"])

    assert rc == 1
    assert calls == []


# ── #5739: none_arg_type_hits_in_tests — the zero-FP, zero-baseline gate ────


def test_none_arg_type_hit_is_extracted_from_a_tests_file() -> None:
    """Tier 1: the exact shape architect named — a None literal passed to a
    declared non-Optional argument, under tests/."""
    module = _load()
    text = (
        'tests/runtime/test_foo.py:42: error: Argument "bar" to "Baz" has '
        'incompatible type "None"; expected "Qux"  [arg-type]\n'
    )
    assert module.none_arg_type_hits_in_tests(text) == {
        (
            "tests/runtime/test_foo.py", 42,
            'Argument "bar" to "Baz" has incompatible type "None"; expected "Qux"',
        )
    }


def test_a_src_file_hit_is_never_counted_even_if_shaped_identically() -> None:
    """Tier 1: FALSIFY — this gate is tests/-scoped ONLY (architect's own
    ruling: the general src/reyn arg-type population stays on the existing,
    baselined ratchet). A followed-import error landing in src/reyn must
    never leak into a gate with NO baseline at all."""
    module = _load()
    text = (
        'src/reyn/foo.py:10: error: Argument "bar" to "Baz" has incompatible '
        'type "None"; expected "Qux"  [arg-type]\n'
    )
    assert module.none_arg_type_hits_in_tests(text) == set()


def test_a_non_none_arg_type_hit_in_tests_is_not_counted() -> None:
    """Tier 1: FALSIFY — an ordinary (non-None) [arg-type] mismatch under
    tests/ is exactly the structural-false-positive population architect
    rejected baselining wholesale; this gate must stay narrow to it."""
    module = _load()
    text = (
        'tests/runtime/test_foo.py:1: error: Argument "bar" to "Baz" has '
        'incompatible type "_Fake"; expected "RealThing"  [arg-type]\n'
    )
    assert module.none_arg_type_hits_in_tests(text) == set()


def test_a_none_hit_under_a_different_code_is_not_counted() -> None:
    """Tier 1: FALSIFY — the shape is scoped to [arg-type] specifically
    (mypy's own literal-None-argument message), not any error mentioning
    the word None."""
    module = _load()
    text = 'tests/runtime/test_foo.py:1: error: Item "None" of "X | None" has no attribute "y"  [union-attr]\n'
    assert module.none_arg_type_hits_in_tests(text) == set()


def test_two_none_hits_on_the_same_line_are_kept_distinct() -> None:
    """Tier 1: a multi-line call passing None for 2+ positional args mypy
    reports as separate lines already collapses correctly since each
    carries its own line number and message — distinct entries, not one."""
    module = _load()
    text = (
        'tests/x.py:5: error: Argument 2 to "f" has incompatible type "None"; expected "A"  [arg-type]\n'
        'tests/x.py:5: error: Argument 3 to "f" has incompatible type "None"; expected "B"  [arg-type]\n'
    )
    assert module.none_arg_type_hits_in_tests(text) == {
        ("tests/x.py", 5, 'Argument 2 to "f" has incompatible type "None"; expected "A"'),
        ("tests/x.py", 5, 'Argument 3 to "f" has incompatible type "None"; expected "B"'),
    }


def test_scope_hides_a_none_arg_hit_in_an_unchanged_test_file() -> None:
    """Tier 1: #5882 — same shape as `new_findings`'s own `scope` above,
    for the #5739 gate: a hit outside changed-files mode's tests/ scope
    (mypy following an import into an unchanged test helper) stays
    invisible on THIS run; unscoped (`--full`) still catches it."""
    module = _load()
    text = (
        'tests/changed.py:1: error: Argument "x" to "Y" has incompatible '
        'type "None"; expected "Z"  [arg-type]\n'
        'tests/unchanged.py:2: error: Argument "a" to "B" has incompatible '
        'type "None"; expected "C"  [arg-type]\n'
    )
    scoped = module.none_arg_type_hits_in_tests(text, scope={"tests/changed.py"})
    assert scoped == {
        ("tests/changed.py", 1, 'Argument "x" to "Y" has incompatible type "None"; expected "Z"'),
    }
    unscoped = module.none_arg_type_hits_in_tests(text, scope=None)
    assert unscoped == {
        ("tests/changed.py", 1, 'Argument "x" to "Y" has incompatible type "None"; expected "Z"'),
        ("tests/unchanged.py", 2, 'Argument "a" to "B" has incompatible type "None"; expected "C"'),
    }


def test_main_fails_on_a_none_arg_type_hit_even_when_the_baselined_ratchet_is_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Tier 1: FALSIFY — #5739's own gate has NO baseline, so it must fail
    `main()` independently of the src/reyn ratchet's own verdict (a clean
    baselined run must not mask a real #5739 hit)."""
    module = _load()
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(module, "load_baseline", lambda: {("src/reyn/foo.py", "attr-defined")})
    monkeypatch.setattr(module, "run_mypy", lambda *a, **kw: "src/reyn/foo.py:1: error: msg  [attr-defined]\n")
    monkeypatch.setattr(
        module, "run_mypy_tests_none_arg_type",
        lambda *a, **kw: 'tests/runtime/test_foo.py:1: error: Argument "x" to "Y" has '
                'incompatible type "None"; expected "Z"  [arg-type]\n',
    )

    rc = module.main(["--full"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "#5739 gate FAILED" in err
    assert "tests/runtime/test_foo.py:1" in err


def test_main_ok_when_both_the_ratchet_and_the_5739_gate_are_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 1: the ordinary green path — neither gate has anything to
    report."""
    module = _load()
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(module, "load_baseline", lambda: {("src/reyn/foo.py", "attr-defined")})
    monkeypatch.setattr(module, "run_mypy", lambda *a, **kw: "src/reyn/foo.py:1: error: msg  [attr-defined]\n")
    monkeypatch.setattr(module, "run_mypy_tests_none_arg_type", lambda *a, **kw: "")

    assert module.main(["--full"]) == 0


def test_main_write_baseline_still_surfaces_a_5739_hit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Tier 1: FALSIFY — #5739 has no baseline to write, so
    `--write-baseline` must not read as "everything is clean" while a real
    #5739 hit exists; the OTHER (src/reyn) baseline still gets written
    (this gate's absence of a baseline is not a reason to block an
    unrelated, legitimate baseline regeneration), but main() still exits
    nonzero and reports the hit."""
    module = _load()
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(module, "write_baseline", lambda pairs, path=None: None)
    monkeypatch.setattr(module, "run_mypy", lambda *a, **kw: "src/reyn/foo.py:1: error: msg  [attr-defined]\n")
    monkeypatch.setattr(
        module, "run_mypy_tests_none_arg_type",
        lambda *a, **kw: 'tests/runtime/test_foo.py:1: error: Argument "x" to "Y" has '
                'incompatible type "None"; expected "Z"  [arg-type]\n',
    )

    rc = module.main(["--write-baseline"])

    assert rc == 1
    assert "#5739 gate FAILED" in capsys.readouterr().err
