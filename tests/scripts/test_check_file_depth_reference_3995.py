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


# ── #4019 — nested INSIDE a peer directory, not just directly under it ──────
# tui-coder's pre-move audit found a real miss: `Path(__file__).parent /
# "fixtures" / "llm" / "fp0063_arc_witness"` resolves to `tests/fixtures/llm/
# fp0063_arc_witness` — nested several levels inside the peer `tests/fixtures`
# directory, not sitting directly under tests_dir. The first version of (b)
# only checked `target.parent == tests_dir`, which never fires for a target
# this deep; it required generalizing to "does the target stay inside the
# file's own home subdirectory, at any depth" (see `_own_home`).


def test_a_target_nested_several_levels_inside_a_peer_is_flagged(tmp_path: Path) -> None:
    """Tier 2: #4019 — THE real miss (`tests/test_fp0063_arc_witness.py`'s
    `Path(__file__).parent / "fixtures" / "llm" / "fp0063_arc_witness"`).
    A target several levels inside a peer directory is exactly as
    position-dependent as one directly under tests_dir — moving the file
    changes where the WHOLE chain resolves, not just its first segment."""
    (tmp_path / "fixtures" / "llm" / "fp0063_arc_witness").mkdir(parents=True)
    (tmp_path / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_DIR = Path(__file__).parent / "fixtures" / "llm" / "fp0063_arc_witness"\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "test_a.py"]


def test_a_target_nested_inside_the_files_own_bucket_is_not_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity for #4019's generalization — a file ALREADY
    living in a subdirectory (a bucket) referencing something nested
    several levels inside THAT SAME bucket is safe: the whole bucket moves
    together under M4, so a reference deep inside it travels with the
    file just as much as a shallow one does."""
    sub = tmp_path / "core"
    (sub / "data" / "nested").mkdir(parents=True)
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_DIR = Path(__file__).parent / "data" / "nested"\n',
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_bare_parent_reference_alone_is_never_flagged_even_for_a_flat_file(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity for the #4019 fix's own precision — a BARE
    `Path(__file__).parent` (no further join at all) must stay safe even
    for a flat file, where it resolves to tests_dir itself. This is a
    trivial self-reference ("my own current directory"), not a peer
    reference — the #4019 generalization must not regress this into a
    false positive (a real regression caught locally before this was
    corrected: an early version flagged this)."""
    (tmp_path / "test_a.py").write_text(
        "from pathlib import Path\n"
        "_HERE = Path(__file__).parent\n",
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_bare_parent_reference_alone_is_never_flagged_for_a_nested_file(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity, the nested-file sibling of the case above — a
    bare `Path(__file__).parent` for a file already in a subdirectory
    resolves to that subdirectory itself, also a trivial self-reference."""
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        "_HERE = Path(__file__).parent\n",
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


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


# ── #4019 (review finding) — a WITHIN-home reference can still be silently
# wrong, invisible to both (a)/(b) above and a′ (which only runs against a
# migration PR's OWN diff, not retroactively against already-merged
# history). Real instance: tests/dev/test_replay_fixture_no_stacking_3634.py
# resolved a module-level glob root to a directory that plainly did not
# exist, silently collecting zero fixture files instead of failing.


def test_a_missing_module_level_glob_root_is_flagged(tmp_path: Path) -> None:
    """Tier 2: #4019's real confirmed instance, reproduced — a
    module-level `_ROOT = Path(__file__).parent / "fixtures"` immediately
    used as `_ROOT.rglob(...)`'s base, where `fixtures/` does not exist,
    is flagged even though it is structurally "inside home" (so (a)/(b)
    alone would have missed it — this is a genuinely separate check).

    Uses a file already living in a subdirectory (own home != tests_dir),
    same isolation reason as the "not flagged" tests below: a FLAT file's
    `.parent / "fixtures"` join is ALSO independently caught by the OLDER
    (a)/(b) peer-directory check regardless of existence, which would make
    this test pass even with the NEW existence check fully stripped — a
    real gap caught locally (falsify-verification here initially showed
    green with the mechanism disabled, for exactly this reason) before
    this isolation fix."""
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_ROOT = Path(__file__).parent / "fixtures"\n'
        '_FILES = sorted(_ROOT.rglob("*.jsonl"))\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [sub / "test_a.py"]


def test_an_existing_module_level_glob_root_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — the SAME shape as above, but the referenced
    directory genuinely exists, must not be flagged. Uses a file already
    living in a subdirectory (own home != tests_dir) so ONLY the new
    existence check is exercised, isolated from the older (a)/(b)
    peer-directory check (which would separately flag a FLAT file's
    `.parent / "fixtures"` join regardless of existence — a different
    check, not what this test is verifying)."""
    sub = tmp_path / "core"
    (sub / "fixtures").mkdir(parents=True)
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_ROOT = Path(__file__).parent / "fixtures"\n'
        '_FILES = sorted(_ROOT.rglob("*.jsonl"))\n',
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_runtime_created_glob_root_inside_a_function_is_not_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity for the design's key constraint (lead-coder's
    #4019 review) — a directory a test CREATES and globs at runtime,
    inside a function body (never at module level, since nothing has run
    yet at import time), must NOT be flagged even though the directory
    genuinely doesn't exist at static-scan time. This is exactly the
    common pattern a blanket existence-assert would have false-positived
    on; restricting to module-level roots is what avoids it. Uses a
    subdirectory file for the same isolation reason as the test above."""
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def test_x(tmp_path):\n"
        '    out_dir = Path(__file__).parent / "generated_output"\n'
        "    out_dir.mkdir()\n"
        '    files = sorted(out_dir.glob("*.txt"))\n',
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_bound_name_module_level_glob_root_is_flagged(tmp_path: Path) -> None:
    """Tier 2: non-vacuity for the name-binding path specifically —
    `_ROOT.rglob(...)` (a Name reference to a module-level-bound variable)
    must be caught the same way a direct `Path(__file__)...rglob(...)`
    chain would be; this is the exact shape of the real #4019 instance
    (`_FIXTURES_ROOT = ...; _FIXTURE_FILES = sorted(_FIXTURES_ROOT.rglob(...))`).
    Same subdirectory isolation as the tests above (the older (a)/(b)
    check would independently catch a flat file's version of this)."""
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "llm"\n'
        '_FIXTURE_FILES = sorted(_FIXTURES_ROOT.rglob("*.jsonl"))\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [sub / "test_a.py"]


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
