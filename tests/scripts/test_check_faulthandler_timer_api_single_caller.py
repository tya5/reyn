"""Tier 1: `check_faulthandler_timer_api_single_caller.py`'s own detector
logic -- the #5978 regression ratchet for "the set of modules calling
faulthandler's timer API (`faulthandler.dump_traceback_later`/
`faulthandler.cancel_dump_traceback_later`) is exactly
`{src/reyn/runtime/stall_trace.py}`".

Both directions are witnessed, same shape as
`test_check_unfiltered_caplog_consumption.py` / `test_silent_except_
ratchet_5990.py`: a real tmp git repo (no MagicMock) standing in for the
`src/` population, once with exactly one caller module (matching real
`stall_trace.py`'s shape) and once with a second module also calling the
timer API -- the gate must pass the first and fail the second, naming the
offending file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_faulthandler_timer_api_single_caller import (
    _SOLE_CALLER,
    find_violations,
    measured,
)
from tests._support.paths import REPO_ROOT

_SOLE_CALLER_BODY = '''\
"""Stand-in for the real sole caller."""
import faulthandler


def arm(seconds: float) -> None:
    faulthandler.dump_traceback_later(seconds, repeat=False)


def disarm() -> None:
    faulthandler.cancel_dump_traceback_later()
'''

_SECOND_CALLER_BODY = '''\
"""A second module that should never exist for real -- this is the
violation shape the gate must catch."""
import faulthandler


def watch(seconds: float) -> None:
    faulthandler.dump_traceback_later(seconds)
'''


def _make_git_tree(tmp_path: Path, *, second_caller: bool) -> Path:
    """A real tmp git repo with `src/reyn/runtime/stall_trace.py` (the
    sole sanctioned caller) and, if *second_caller*, an additional
    `src/reyn/other_module.py` that ALSO calls the timer API -- `git
    ls-files` needs the files staged (not necessarily committed) to be
    counted, matching the production population source exactly."""
    root = tmp_path / "repo"
    sole_caller_path = root / _SOLE_CALLER
    sole_caller_path.parent.mkdir(parents=True)
    sole_caller_path.write_text(_SOLE_CALLER_BODY, encoding="utf-8")

    if second_caller:
        other = root / "src" / "reyn" / "other_module.py"
        other.write_text(_SECOND_CALLER_BODY, encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_exactly_one_caller_module_passes(tmp_path: Path) -> None:
    """Tier 1: the false-accept-side witness -- a tree shaped like the
    real one (only `stall_trace.py` calls the timer API) reports zero
    violations."""
    root = _make_git_tree(tmp_path, second_caller=False)
    by_file = measured(root)
    others = {f: v for f, v in by_file.items() if f != _SOLE_CALLER}
    assert others == {}, f"gate falsely flagged the sole sanctioned caller's tree: {others}"
    assert by_file[_SOLE_CALLER], "the sole caller's own call sites went undetected"


def test_a_second_caller_module_fails_naming_the_file(tmp_path: Path) -> None:
    """Tier 1: the false-reject-side witness, required alongside the test
    above -- a SECOND module calling the timer API must be flagged, and
    the offending file must be named (not merely "something failed")."""
    root = _make_git_tree(tmp_path, second_caller=True)
    by_file = measured(root)
    others = {f: v for f, v in by_file.items() if f != _SOLE_CALLER}
    assert "src/reyn/other_module.py" in others, (
        f"gate missed the second caller module entirely: {by_file}"
    )
    lineno, api = others["src/reyn/other_module.py"][0]
    assert api == "dump_traceback_later"
    assert lineno > 0


def test_find_violations_detects_both_timer_api_names(tmp_path: Path) -> None:
    """Tier 1: both `dump_traceback_later` and `cancel_dump_traceback_
    later` are detected by name, on a single real file -- not just
    whichever one the fixtures above happen to exercise."""
    f = tmp_path / "both.py"
    f.write_text(
        "import faulthandler\n"
        "faulthandler.dump_traceback_later(5)\n"
        "faulthandler.cancel_dump_traceback_later()\n",
        encoding="utf-8",
    )
    violations = find_violations(f)
    apis = {api for _, api in violations}
    assert apis == {"dump_traceback_later", "cancel_dump_traceback_later"}


def test_a_docstring_or_comment_naming_the_api_is_not_flagged(tmp_path: Path) -> None:
    """Tier 1: the classifier is AST-based (a real `Call` node), not a
    text/regex match -- a docstring or comment merely naming the API must
    not be flagged."""
    f = tmp_path / "prose_only.py"
    f.write_text(
        '"""Mentions faulthandler.dump_traceback_later(...) in prose."""\n'
        "# faulthandler.cancel_dump_traceback_later() is also just a comment here.\n",
        encoding="utf-8",
    )
    assert find_violations(f) == []


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population, verified against the
    real, current tree (not assumed) -- this is the "already green"
    witness: `src/reyn/runtime/stall_trace.py` must be the ONLY caller of
    faulthandler's timer API under `src/` today. Any other hit here is a
    real regression, not inherited debt (there is no baseline for this
    gate)."""
    by_file = measured(REPO_ROOT)
    others = {f: v for f, v in by_file.items() if f != _SOLE_CALLER}
    assert others == {}, (
        f"real regression(s) found -- a module other than {_SOLE_CALLER} "
        f"now calls faulthandler's timer API: {others}"
    )
    assert _SOLE_CALLER in by_file, (
        f"{_SOLE_CALLER} itself no longer calls faulthandler's timer API -- "
        "either it was refactored away (update this gate deliberately) or "
        "the scan is broken"
    )
