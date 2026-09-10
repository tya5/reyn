"""Tier 1: `check_media_store_unlink_single_caller.py`'s own detector
logic -- the #6050 regression ratchet for "the only place inside
`media_store.py` allowed to call `Path.unlink()` is
`MediaStore._unlink_tracked`".

Same 2-direction shape as `test_check_faulthandler_timer_api_single_
caller.py`, PLUS a third direction lead-coder's own review found
missing from an earlier draft of this gate: a tree where the SANCTIONED
caller itself was renamed/removed must fail, not report a vacuous OK
(the exact class of bug #6050's own production fix closes, now
re-opened at the gate level if left unchecked).
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_media_store_unlink_single_caller import (
    _SOLE_CALLER_METHOD,
    find_calls,
    main,
)
from tests._support.paths import REPO_ROOT

_SANCTIONED_BODY = '''\
"""Stand-in for the real sole caller shape."""
from pathlib import Path


class MediaStore:
    def _unlink_tracked(self, path: Path) -> int:
        size = path.stat().st_size
        path.unlink()
        self._forget(path)
        return size

    def _forget(self, path: Path) -> None:
        pass
'''

_VIOLATION_BODY = '''\
"""A second unlink call site -- the violation shape this gate must catch."""
from pathlib import Path


class MediaStore:
    def _unlink_tracked(self, path: Path) -> int:
        size = path.stat().st_size
        path.unlink()
        self._forget(path)
        return size

    def _evict_something(self, path: Path) -> None:
        path.unlink()

    def _forget(self, path: Path) -> None:
        pass
'''

_NO_SANCTIONED_CALLER_BODY = '''\
"""_unlink_tracked itself was renamed/removed -- the class of bug this
gate's own review found: `find_calls` returning violations-only would
report zero violations here (nothing calls .unlink() outside a function
named _unlink_tracked, because nothing calls .unlink() at all) and the
gate would falsely claim "_unlink_tracked is the only caller"."""
from pathlib import Path


class MediaStore:
    def _forget(self, path: Path) -> None:
        pass
'''


def test_the_sanctioned_shape_has_no_violations_and_one_sanctioned_call(tmp_path: Path) -> None:
    """Tier 1: the false-accept-side witness -- a file shaped like the
    real one (one `.unlink()` call, inside `_unlink_tracked`) reports
    zero violations and exactly one sanctioned call site."""
    f = tmp_path / "sanctioned.py"
    f.write_text(_SANCTIONED_BODY, encoding="utf-8")
    violations, sanctioned = find_calls(f)
    assert violations == []
    assert sanctioned == [8]


def test_a_second_unlink_call_site_is_flagged_as_a_violation(tmp_path: Path) -> None:
    """Tier 1: the false-reject-side witness -- a SECOND `.unlink()` call
    outside `_unlink_tracked` must be flagged, naming the enclosing
    method."""
    f = tmp_path / "violation.py"
    f.write_text(_VIOLATION_BODY, encoding="utf-8")
    violations, sanctioned = find_calls(f)
    assert sanctioned == [8]
    assert violations == [(13, "_evict_something")]


def test_the_sanctioned_caller_being_removed_fails_not_a_vacuous_ok(tmp_path: Path) -> None:
    """Tier 1: #6050 BLOCKING (lead-coder review) -- a tree where
    `_unlink_tracked` itself was renamed or deleted must make `main()`
    fail (exit 1), not report a vacuous OK. `violations` alone would be
    empty here (nothing calls `.unlink()` at all) -- `sanctioned` being
    empty too is what `main()` must check separately.

    Strip-falsify (verified by hand: reverting `main()`'s `if not
    sanctioned: ... return 1` block back to only checking `violations`):
    this test goes red -- `main()` returns 0 on this exact tree."""
    f = tmp_path / "media_store.py"
    f.write_text(_NO_SANCTIONED_CALLER_BODY, encoding="utf-8")
    violations, sanctioned = find_calls(f)
    assert violations == [], "setup: no .unlink() call anywhere in this tree at all"
    assert sanctioned == [], "setup: the sanctioned caller's own call site is genuinely absent"

    import scripts.check_media_store_unlink_single_caller as gate_module
    original_root, original_target = gate_module._ROOT, gate_module._TARGET
    try:
        gate_module._ROOT = tmp_path
        gate_module._TARGET = "media_store.py"
        exit_code = main([])
    finally:
        gate_module._ROOT, gate_module._TARGET = original_root, original_target
    assert exit_code == 1, (
        "main() must fail when the sanctioned caller itself is absent, "
        "not report 'X is the only caller' vacuously true of an empty set"
    )


def test_a_docstring_or_comment_naming_unlink_is_not_flagged(tmp_path: Path) -> None:
    """Tier 1: the classifier is AST-based (a real `Call` node), not a
    text/regex match."""
    f = tmp_path / "prose_only.py"
    f.write_text(
        '"""Mentions .unlink() in prose."""\n'
        "# path.unlink() is also just a comment here.\n",
        encoding="utf-8",
    )
    violations, sanctioned = find_calls(f)
    assert violations == []
    assert sanctioned == []


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population, verified against the
    real, current tree (not assumed) -- `_unlink_tracked` is the ONLY
    caller of `.unlink()` in `media_store.py` today."""
    target = REPO_ROOT / "src/reyn/data/workspace/media_store.py"
    violations, sanctioned = find_calls(target)
    assert violations == [], (
        f"real regression(s) found -- a method other than "
        f"{_SOLE_CALLER_METHOD} now calls .unlink(): {violations}"
    )
    assert sanctioned, (
        f"{_SOLE_CALLER_METHOD} itself no longer calls .unlink() -- either "
        "it was refactored away (update this gate deliberately) or the "
        "scan is broken"
    )
