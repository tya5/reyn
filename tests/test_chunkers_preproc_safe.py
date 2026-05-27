"""Tier 2: OS invariant tests for chunkers_preproc_safe (FP-0042 Phase 2.1).

Tests the safe-mode preprocessor steps (``gather_samples`` /
``cost_preflight``) that replaced the prior unsafe versions in
``chunkers.py``. Coverage mirrors the equivalent tests in
``test_chunkers.py`` so the migration is observably bit-compatible
at the artifact contract.

No mocks; uses real filesystem operations via tmp_path. The
``_set_permission_context`` autouse fixture establishes a read grant
over ``tmp_path`` so the safe-mode file API can be exercised
directly (= same pattern as ``test_safe_file_api.py``).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from reyn.safe import file as sf


def _load_chunkers_preproc_safe():
    """Import chunkers_preproc_safe.py from the skill directory."""
    skill_dir = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_docs"
    )
    spec = importlib.util.spec_from_file_location(
        "chunkers_preproc_safe", skill_dir / "chunkers_preproc_safe.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_C = _load_chunkers_preproc_safe()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_safe_file_context():
    """Reset reyn.safe.file's module-global permission context per test."""
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    yield
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False


@pytest.fixture
def grant_tmp_read(tmp_path: Path):
    """Grant safe.file read permission over ``tmp_path``.

    Mirrors the production wiring where ``preprocessor_executor`` pre-pends
    the CWD as a default read zone so a safe-mode step can read
    workspace-local files without explicit declarations.
    """
    sf._set_permission_context(read_paths=[str(tmp_path)])
    return tmp_path


def _make_artifact(data: dict) -> dict:
    """Wrap data in the standard artifact envelope."""
    return {"type": "index_docs_input", "data": data}


# ---------------------------------------------------------------------------
# gather_samples
# ---------------------------------------------------------------------------


def test_gather_samples_empty_path_returns_empty(grant_tmp_read: Path):
    """Tier 2: gather_samples on a non-existent path returns empty samples."""
    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "nonexistent" / "**" / "*.md"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    assert result["samples"] == []
    assert result["file_count"] == 0
    assert result["summary"]["file_count"] == 0
    assert result["summary"]["total_bytes"] == 0


def test_gather_samples_stratified_by_extension(grant_tmp_read: Path):
    """Tier 2: gather_samples picks samples per extension (stratified)."""
    for i in range(3):
        (grant_tmp_read / f"doc{i}.md").write_text(
            f"# Heading {i}\nContent {i}", encoding="utf-8"
        )
    for i in range(2):
        (grant_tmp_read / f"script{i}.py").write_text(
            f"def fn{i}():\n    pass", encoding="utf-8"
        )

    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "**" / "*"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    samples = result["samples"]
    exts = {Path(s["path"]).suffix for s in samples}
    assert ".md" in exts
    assert ".py" in exts
    assert result["file_count"] == 5
    assert result["summary"]["file_count"] == 5


def test_gather_samples_respects_sample_size_cap(grant_tmp_read: Path):
    """Tier 2: gather_samples respects the sample_size cap (default 5)."""
    for i in range(10):
        (grant_tmp_read / f"doc{i}.md").write_text(
            f"# Doc {i}\nSome content.", encoding="utf-8"
        )

    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "*.md"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    assert result["samples"], "expected at least one sample"
    assert result["file_count"] == 10
    # Cap must be honoured: fewer samples than total files.
    assert len(result["samples"]) < result["file_count"]


def test_gather_samples_structure_hint_for_markdown(grant_tmp_read: Path):
    """Tier 2: structure_hint is 'Markdown with headings' for .md with # headings."""
    (grant_tmp_read / "readme.md").write_text(
        "# Title\n\n## Section\n\nBody text.", encoding="utf-8"
    )
    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "*.md"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    assert result["samples"], "expected a sample for the single .md file"
    assert result["samples"][0]["structure_hint"] == "Markdown with headings"


def test_gather_samples_structure_hint_for_python(grant_tmp_read: Path):
    """Tier 2: structure_hint mentions Python for .py with class/def."""
    (grant_tmp_read / "module.py").write_text(
        "class MyClass:\n    def method(self):\n        pass",
        encoding="utf-8",
    )
    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "*.py"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    assert result["samples"], "expected a sample for the single .py file"
    assert "Python" in result["samples"][0]["structure_hint"]


def test_gather_samples_summary_ext_dist(grant_tmp_read: Path):
    """Tier 2: ext_dist counts files per extension."""
    (grant_tmp_read / "a.md").write_text("hello", encoding="utf-8")
    (grant_tmp_read / "b.md").write_text("world", encoding="utf-8")
    (grant_tmp_read / "c.py").write_text("x = 1", encoding="utf-8")

    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "*"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    ext_dist = result["summary"]["ext_dist"]
    assert ext_dist.get(".md") == 2
    assert ext_dist.get(".py") == 1


def test_gather_samples_filters_directories(grant_tmp_read: Path):
    """Tier 2: directories returned by glob are filtered out (= os.path.isfile
    behaviour preserved via reyn.safe.file.stat mode check)."""
    (grant_tmp_read / "subdir").mkdir()
    (grant_tmp_read / "a.md").write_text("content", encoding="utf-8")

    artifact = _make_artifact(
        {"path": str(grant_tmp_read / "*"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    # Only "a.md" is a regular file; "subdir" should not appear.
    assert result["file_count"] == 1
    assert result["summary"]["ext_dist"].get(".md") == 1
    # No entry for the (extension-less) directory should leak into ext_dist.
    assert "" not in result["summary"]["ext_dist"]


def test_gather_samples_denied_path_returns_empty(tmp_path: Path):
    """Tier 2: when the path is outside the granted read zone, the safe
    file API raises PermissionError → samples skip silently (matching
    legacy ``os.path.isfile`` False / ``open`` OSError behaviour).
    Net effect: ``file_count`` reflects glob's view but ``samples`` /
    sizes stay empty because every stat / read fails the permission
    check.
    """
    (tmp_path / "denied.md").write_text("secret", encoding="utf-8")
    # Grant a *different* directory than the one we'll glob.
    other = tmp_path / "elsewhere"
    other.mkdir()
    sf._set_permission_context(read_paths=[str(other)])

    artifact = _make_artifact(
        {"path": str(tmp_path / "*.md"), "source": "x"}
    )
    result = _C.gather_samples(artifact)

    # stat() raised PermissionError → _is_regular_file returned False →
    # the file was filtered before sampling. file_count is 0 because
    # _glob_files filters through _is_regular_file.
    assert result["samples"] == []
    assert result["file_count"] == 0


# ---------------------------------------------------------------------------
# cost_preflight
# ---------------------------------------------------------------------------


def test_cost_preflight_empty_samples_returns_zero(grant_tmp_read: Path):
    """Tier 2: cost_preflight with empty samples returns zero cost."""
    artifact = _make_artifact(
        {
            "path": str(grant_tmp_read / "missing" / "**"),
            "source": "x",
            "samples_result": {"samples": [], "summary": {}, "file_count": 0},
        }
    )
    result = _C.cost_preflight(artifact)

    assert result["chunk_count"] == 0
    assert result["estimated_tokens"] == 0
    assert result["estimated_cost_usd"] == 0.0
    assert result["threshold_exceeded"] is False


def test_cost_preflight_threshold_exceeded_flag(grant_tmp_read: Path):
    """Tier 2: threshold_exceeded flips when estimated chunks > threshold."""
    for i in range(100):
        (grant_tmp_read / f"doc{i}.md").write_text("x" * 4000, encoding="utf-8")

    samples = [
        {
            "path": str(grant_tmp_read / "doc0.md"),
            "excerpt": "x" * 1000,
            "size_tokens": 1000,
            "structure_hint": "Markdown without headings",
        }
    ]

    artifact = _make_artifact(
        {
            "path": str(grant_tmp_read / "*.md"),
            "source": "x",
            "cost_warn_threshold": 10,
            "samples_result": {"samples": samples, "file_count": 100},
        }
    )
    result = _C.cost_preflight(artifact)

    assert result["threshold_exceeded"] is True


def test_cost_preflight_not_exceeded_for_few_files(grant_tmp_read: Path):
    """Tier 2: threshold_exceeded stays False for small input sets."""
    (grant_tmp_read / "doc.md").write_text("Short file.", encoding="utf-8")

    samples = [
        {
            "path": str(grant_tmp_read / "doc.md"),
            "excerpt": "Short file.",
            "size_tokens": 3,
            "structure_hint": "Markdown without headings",
        }
    ]

    artifact = _make_artifact(
        {
            "path": str(grant_tmp_read / "*.md"),
            "source": "x",
            "cost_warn_threshold": 10_000,
            "samples_result": {"samples": samples, "file_count": 1},
        }
    )
    result = _C.cost_preflight(artifact)

    assert result["threshold_exceeded"] is False
    assert result["estimated_cost_usd"] >= 0.0


# ---------------------------------------------------------------------------
# path_suffix / detect_structure (internal helpers — pinned because the
# LLM-facing structure_hint string must stay stable across the migration)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("foo.md", ".md"),
        ("/a/b/c.py", ".py"),
        ("noext", ""),
        (".hidden", ""),
        (".hidden.md", ".md"),
        ("a/b/.dotonly", ""),
        ("nested.dir/file.txt", ".txt"),
    ],
)
def test_path_suffix_mirrors_pathlib(path: str, expected: str):
    """Tier 2: path_suffix matches pathlib.PurePath.suffix for our cases."""
    assert _C.path_suffix(path) == expected


@pytest.mark.parametrize(
    "ext, text, expected",
    [
        (".md", "# Heading\nbody", "Markdown with headings"),
        (".md", "plain text only", "Markdown without headings"),
        (".py", "class Foo:\n    pass", "Python with class/function definitions"),
        (".py", "x = 1\nprint(x)", "Python script"),
        (".ts", "const x = 1;", "JavaScript/TypeScript"),
        (".json", "{}", "Structured data file"),
        (".txt", "anything", "Plain text"),
    ],
)
def test_detect_structure_mirrors_unsafe_heuristic(ext: str, text: str, expected: str):
    """Tier 2: detect_structure strings match chunkers.py's legacy heuristic
    exactly — the LLM sees identical structure_hint values across the
    FP-0042 migration."""
    assert _C.detect_structure(text, ext) == expected
