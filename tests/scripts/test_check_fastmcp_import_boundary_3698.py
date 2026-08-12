"""Tier 2: #3698 — the fastmcp-import-boundary enforcement gate.

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files) — the function under test reads real file content and parses real
ASTs, so faking the filesystem would test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_fastmcp_import_boundary import offending_files


def test_a_direct_module_level_import_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE case #3698 P2/P3 warned would go unnoticed — a future
    direct `import fastmcp` reintroduced anywhere under this directory."""
    (tmp_path / "client.py").write_text("import fastmcp\n", encoding="utf-8")
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "client.py"]


def test_a_from_fastmcp_submodule_import_is_flagged(tmp_path: Path) -> None:
    """Tier 2: `from fastmcp.client.auth import OAuth` shape — a submodule
    import, not the bare package name."""
    (tmp_path / "client.py").write_text(
        "from fastmcp.client.auth import OAuth\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "client.py"]


def test_a_deferred_function_local_import_is_also_flagged(tmp_path: Path) -> None:
    """Tier 2: a deferred (function-local) import is in scope too — a NEW
    direct import must be caught regardless of whether it's module-level
    or deferred, since a regression could reach for either shape."""
    (tmp_path / "client.py").write_text(
        "def f():\n    from fastmcp import Client\n    return Client\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "client.py"]


def test_an_import_of_an_unrelated_package_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: `import mcp.types` (the lower-level protocol-spec package
    fastmcp itself wraps, not fastmcp) must not false-positive — only a
    module named exactly `fastmcp` or `fastmcp.<sub>` counts."""
    (tmp_path / "message_handler.py").write_text(
        "import mcp.types\nfrom mcp.types import ServerNotification\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_a_package_named_fastmcp_prefixed_is_not_confused_with_fastmcp(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity for the exact-match / dotted-prefix rule — a
    hypothetical unrelated package whose name merely STARTS WITH the same
    letters (e.g. `fastmcplib`, not a real package, but the rule must not
    match on a bare substring) is not flagged."""
    (tmp_path / "client.py").write_text("import fastmcplib\n", encoding="utf-8")
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current tree (not assumed), matching the sibling gates' own
    "run it before shipping it" discipline. #3698 P3 removed the last
    remaining direct fastmcp import (message_handler.py's inheritance-based
    one); #4302 later confirmed the client stack's fastmcp dependency is
    gone entirely, not just relocated. This asserts it stayed removed."""
    from scripts.check_fastmcp_import_boundary import _MCP_DIR, _ROOT

    assert _MCP_DIR == _ROOT / "src" / "reyn" / "mcp"
    offenders = offending_files(_MCP_DIR)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
