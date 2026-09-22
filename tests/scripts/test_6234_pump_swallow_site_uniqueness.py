"""Tier 2: #6234 gate — scripts/check_pump_swallow_site_uniqueness.py.

Architect ruling (#6234, issuecomment-5772189312): `_record_pump_swallow`'s
dedup key is `(site, exception type)`, where `site` is a hand-written
static literal, never derived from a detector. The one real risk a
hand-written literal carries is a copy-paste duplicate — two call sites
sharing the same `site` collapse into one dedup bucket, and whichever
fires first silently suppresses the audit-event for the other. This file
pins the AST gate that catches that: a synthetic fixture module with a
genuine duplicate must fail BOTH the underlying detector (find_violations)
and main()'s own CLI exit code — mirrors test_5131_tui_widget_boundary.py's
own "no witness on an empty/already-compliant fixture" discipline
(CLAUDE.md test-review question 4: a green gate that never had anything to
bite on wears the same colour as one that is wired correctly).

Real files on a real tmp_path — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import scripts.check_pump_swallow_site_uniqueness as check_pump_swallow_site_uniqueness
from scripts.check_pump_swallow_site_uniqueness import find_violations, main


def _write(target: Path, content: str) -> None:
    target.write_text(content, encoding="utf-8")


def test_find_violations_is_empty_on_a_fixture_with_distinct_literals(
    tmp_path: Path,
) -> None:
    """Tier 2: deny-side witness — an assertion over an EMPTY collection is
    not itself a check (CLAUDE.md test review, row 4), so this must be
    paired with the present-side test below in the same file, never stand
    alone. Two call sites, two DIFFERENT string literals: no finding."""
    fixture = tmp_path / "app.py"
    _write(
        fixture,
        (
            "class App:\n"
            "    def f(self):\n"
            "        self._record_pump_swallow('site_a', exc)\n"
            "        self._record_pump_swallow('site_b', exc)\n"
        ),
    )

    duplicates, non_literal_lines = find_violations(fixture)

    assert duplicates == []
    assert non_literal_lines == []


def test_find_violations_catches_a_reused_literal(tmp_path: Path) -> None:
    """Tier 2: present-side witness — the exact defect class this gate
    exists to catch: two call sites passing the SAME literal. Falsification
    contrast to the test above (same shape, one string changed).

    Strip-falsified in-file (Edit only, ``if site in seen:`` temporarily
    replaced with ``if False:``): RED with ``AssertionError: assert [] ==
    [(4, 'site_a')]``. Restored, confirmed GREEN."""
    fixture = tmp_path / "app.py"
    _write(
        fixture,
        (
            "class App:\n"
            "    def f(self):\n"
            "        self._record_pump_swallow('site_a', exc)\n"
            "        self._record_pump_swallow('site_a', exc)\n"
        ),
    )

    duplicates, non_literal_lines = find_violations(fixture)

    assert duplicates == [(4, "site_a")]
    assert non_literal_lines == []


def test_find_violations_catches_a_non_literal_first_argument(tmp_path: Path) -> None:
    """Tier 2: the SECOND finding shape — a variable (or any non-string-
    constant expression) as the first argument defeats the whole premise
    ("a duplicate is visible in the diff"), so it is flagged even with no
    literal collision present."""
    fixture = tmp_path / "app.py"
    _write(
        fixture,
        (
            "class App:\n"
            "    def f(self, kind):\n"
            "        self._record_pump_swallow(kind, exc)\n"
        ),
    )

    duplicates, non_literal_lines = find_violations(fixture)

    assert duplicates == []
    assert non_literal_lines == [3]


def test_find_violations_ignores_a_docstring_merely_mentioning_the_method(
    tmp_path: Path,
) -> None:
    """Tier 2: AST-based (real ``Call`` nodes), not substring — a docstring
    that MENTIONS ``_record_pump_swallow`` in prose (this package's own
    module docstring does, repeatedly) must never false-positive."""
    fixture = tmp_path / "app.py"
    _write(
        fixture,
        (
            '"""Calls _record_pump_swallow at several sites, see below."""\n'
            "class App:\n"
            "    def f(self):\n"
            "        pass\n"
        ),
    )

    duplicates, non_literal_lines = find_violations(fixture)

    assert duplicates == []
    assert non_literal_lines == []


def test_main_exits_nonzero_on_a_fixture_with_a_duplicate_site(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the blocking gap itself — a green find_violations() result
    is not evidence main()'s own CLI wiring ever reaches it. Monkeypatches
    the MODULE-LEVEL ``_TARGET`` (main() does a fresh name lookup, not a
    bound default — see find_violations's own docstring) and drives main()
    itself, asserting the real CLI exit code.

    Strip-falsified in-file (Edit only, same ``if site in seen:`` ->
    ``if False:`` change as the test above): RED with ``AssertionError:
    main() did not reject a fixture with a duplicate site / assert 0 ==
    1``. Restored, confirmed GREEN."""
    fixture = tmp_path / "app.py"
    _write(
        fixture,
        (
            "class App:\n"
            "    def f(self):\n"
            "        self._record_pump_swallow('dup', exc)\n"
            "        self._record_pump_swallow('dup', exc)\n"
        ),
    )
    monkeypatch.setattr(check_pump_swallow_site_uniqueness, "_TARGET", fixture)

    exit_code = main()

    assert exit_code == 1, "main() did not reject a fixture with a duplicate site"


def test_main_exits_zero_on_a_compliant_fixture(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: falsification contrast for the test above — a fixture with
    NO violation still passes through main()'s full CLI path to exit 0, so
    the red witness above is pinned to the violation, not to some other
    difference between the fixture and the real file."""
    fixture = tmp_path / "app.py"
    _write(
        fixture,
        (
            "class App:\n"
            "    def f(self):\n"
            "        self._record_pump_swallow('only_one', exc)\n"
        ),
    )
    monkeypatch.setattr(check_pump_swallow_site_uniqueness, "_TARGET", fixture)

    assert main() == 0


def test_main_exits_zero_on_the_real_app_py() -> None:
    """Tier 2: the real, current ``app.py`` — all 21 ``_record_pump_swallow``
    call sites landed by #6234 use pairwise-distinct static literals. This
    is the gate actually protecting the production file, not only a
    synthetic fixture."""
    assert main() == 0
