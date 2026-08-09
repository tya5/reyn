"""Tier 2: #3995/#4002 — the file-depth-reference gate (static/add-time half).

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files, real directories) — the function under test reads real file
content and compares real Path objects, so faking the filesystem would
test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_file_depth_reference import offending_files


def test_a_repo_root_escaping_file_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE unambiguous case — a __file__ reference that reaches
    ABOVE tests/ entirely (the repo root, or beyond) is always flagged;
    reaching outside tests/ is never something an individual file carries
    with it."""
    (tmp_path / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "test_a.py"]


def test_a_tests_root_child_reference_is_flagged(tmp_path: Path) -> None:
    """Tier 2: #4002 — THE exact real-world instance that forced this
    redesign: `Path(__file__).parent / "_support"`, only ONE hop, still
    breaks on a move because `_support` is a tests/-root child that does
    not travel with an individual file."""
    (tmp_path / "_support").mkdir()
    (tmp_path / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_SUPPORT = Path(__file__).parent / "_support"\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "test_a.py"]


def test_the_child_directory_check_needs_no_curated_name(tmp_path: Path) -> None:
    """Tier 2: non-vacuity for #4002's "FS-derived, not curated" design —
    a directory name NEVER SEEN before (not `_support`, not `fixtures`,
    not anything hardcoded anywhere) is still caught, because the check is
    a structural `target.parent == tests_dir` comparison against the real
    filesystem, not a name-membership test."""
    (tmp_path / "some_brand_new_bucket_name").mkdir()
    (tmp_path / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_X = Path(__file__).parent / "some_brand_new_bucket_name"\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "test_a.py"]


def test_a_co_located_reference_from_a_subdirectory_file_is_not_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity — a reference that stays strictly WITHIN the
    file's own directory, from a file that ALREADY lives in a
    subdirectory (so its own directory is NOT tests_dir itself), must not
    be flagged; this is the ordinary, common co-located-fixture usage this
    gate must not over-fire on."""
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_FIXTURE = Path(__file__).parent / "my_own_fixture.json"\n',
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_flat_files_own_sibling_reference_is_still_flagged(tmp_path: Path) -> None:
    """Tier 2: #4002's real confirmed instance was a FLAT tests/ file
    (`test_2608_h1_mcp_resource_updated_hook.py`, living directly in
    tests/) — for a file whose OWN directory already IS tests_dir, a
    `.parent / <anything>` reference is structurally INDISTINGUISHABLE
    from a reference to a tests/-root peer directory (both resolve to a
    direct child of tests_dir) — architect's own "undecidable from source
    text alone" finding applies here specifically. This gate deliberately
    flags this shape from a flat file even for a plausible-looking
    "private fixture" name, erring toward the false positive (a real
    per-test co-located fixture, if this pattern turns out to be
    genuinely needed, is expected to live in a subdirectory instead,
    matching the case above) rather than the false negative — a
    deterrent, not a precise judge (see module docstring)."""
    (tmp_path / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_FIXTURE = Path(__file__).parent / "my_own_fixture.json"\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "test_a.py"]


def test_a_file_with_no_file_reference_at_all_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — an ordinary test with no __file__ usage at all
    passes trivially (the overwhelming majority of tests/ content)."""
    (tmp_path / "test_a.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert offending_files(tmp_path) == []


def test_conftest_py_is_structurally_exempt(tmp_path: Path) -> None:
    """Tier 2: `conftest.py` never moves (pytest requires it at a fixed
    location relative to the tests it configures — M4's own migration
    leaves it in place), so a depth-counted reference there carries none
    of the risk this gate exists to catch. Real instance: the live
    `tests/conftest.py` on this checkout DOES use
    `Path(__file__).resolve().parent.parent` — this exemption is not
    hypothetical."""
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\n"
        "_REPO_ROOT = str(Path(__file__).resolve().parent.parent)\n",
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_subdirectory_conftest_py_is_also_exempt(tmp_path: Path) -> None:
    """Tier 2: non-vacuity for the exemption's scope — pytest resolves the
    NEAREST conftest.py to the file being collected, so a subdirectory can
    legitimately carry its own; the exemption is keyed on the filename,
    not a fixed path."""
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "conftest.py").write_text(
        "from pathlib import Path\n"
        "_REPO_ROOT = str(Path(__file__).resolve().parent.parent)\n",
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_recurses_into_subdirectories(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — the gate scans ALL of tests/, not just the
    top level; a violation in a subdirectory (e.g. tests/_support/, the
    exact class #3998 fixed) must still be caught."""
    (tmp_path / "fixtures").mkdir()
    sub = tmp_path / "_support"
    sub.mkdir()
    (sub / "helper.py").write_text(
        "from pathlib import Path\n"
        '_DIR = Path(__file__).parent.parent / "fixtures"\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [sub / "helper.py"]


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: #3990/#3997/#3998/#4002 — the zero-baseline claim this gate
    is founded on, checked against the ACTUAL checkout, not a fixture. If
    this ever goes red on a clean checkout, the gate's own founding
    premise (baseline 0, no grandfather clause) is false and must be
    re-examined before trusting any other run of this gate."""
    from scripts.check_file_depth_reference import _ROOT, _TESTS_DIR

    assert _TESTS_DIR == _ROOT / "tests"
    offenders = offending_files(_TESTS_DIR)
    assert offenders == [], (
        f"the real tests/ tree is not clean — {len(offenders)} file(s) "
        f"already violate the predicate this gate's zero-baseline premise "
        f"assumes: {offenders}"
    )
