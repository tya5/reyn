"""Pure(ish) facts about where THIS session is running (#5012-A).

Feeds `describe_session`'s "own position" field — repo path / branch / HEAD
/ venv / capability (ruff, pytest, mkdocs availability). Deliberately takes
its inputs explicitly (a repo root `Path`) rather than reaching into
`Session` itself, so it is testable against a real temp git repo without
constructing a full `Session` (mirrors the split
`scripts/check_doc_drift.py` uses between pure logic and its network/
filesystem wrappers — this module IS the filesystem-wrapper half; there is
no meaningful pure half to further split out, since every fact here comes
from either `git` or the running interpreter).

Every subprocess call is tolerant of failure (no git installed, not a git
repo, a detached HEAD) — this reports what it can observe, honestly marking
what it could not, rather than raising and denying the whole tool result
over one unavailable sub-fact.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

# The one tunable knob here: which external tools' presence is reported.
# Scoped to exactly the three the #5012-A reporter named (ruff/pytest/mkdocs)
# — this is CLAUDE.md's own toolchain (ruff for lint, pytest for tests,
# mkdocs for the docs build), not a general "what's on PATH" survey; adding
# a tool here without a matching reporter need would be exactly the
# "increase the population without cause" CLAUDE.md warns against.
_CAPABILITY_TOOLS = ("ruff", "pytest", "mkdocs")


def _run_git(repo_root: Path, *args: str) -> "str | None":
    """One `git` invocation, stdout stripped, or ``None`` on any failure
    (git missing, not a repo, no commits yet, ...) — never raises."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_position(repo_root: Path) -> "Mapping[str, str | None]":
    """``{"branch": ..., "head": ...}`` — each ``None`` if unavailable
    (detached HEAD has no branch name; a fresh repo with no commits has no
    HEAD) rather than a fabricated placeholder."""
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        # `--abbrev-ref` itself returns the literal string "HEAD" when
        # detached — that is not a branch name, so normalize to None
        # rather than reporting "HEAD" as if it were one.
        branch = None
    head = _run_git(repo_root, "rev-parse", "HEAD")
    return {"branch": branch, "head": head}


def venv_position() -> "Mapping[str, str]":
    """``{"python_executable": ..., "venv_path": ...}`` — ``sys.executable``
    and ``sys.prefix`` are always populated (the running interpreter always
    has both), so this never needs an unavailable-marker branch."""
    return {"python_executable": sys.executable, "venv_path": sys.prefix}


def capability_probe(
    *, resolve: "Callable[[str], str | None]" = shutil.which,
) -> "Mapping[str, bool]":
    """``{"ruff": bool, "pytest": bool, "mkdocs": bool}`` — whether each
    tool resolves on ``PATH``. A boolean, not the resolved path: the caller
    (an LLM agent) needs "can I run this", which is what a not-None
    resolution means once collapsed to yes/no — the actual path is an
    implementation detail the report does not need to leak.

    ``resolve`` defaults to ``shutil.which`` (production behaviour,
    unchanged) but is an injectable seam (lead-coder review, #5012-A PR
    #5038 issuecomment-5376723503): reyn's own contract here is "return a
    boolean per named tool derived from the injected resolver's present/
    absent result" — ``shutil.which``'s own PATH-lookup correctness is the
    standard library's responsibility, not reyn's, so a test pinning THIS
    module's contract must be able to construct both a present and an
    absent case without depending on (or re-deriving) ``shutil.which``
    itself."""
    return {tool: resolve(tool) is not None for tool in _CAPABILITY_TOOLS}


def describe_session_position(repo_root: Path) -> dict:
    """The full "own position" bundle: repo root, branch, HEAD, venv, and
    tool capability — the one field this module exists to build."""
    git = git_position(repo_root)
    venv = venv_position()
    capability = capability_probe()
    return {
        "repo_root": str(repo_root),
        "branch": git["branch"],
        "head": git["head"],
        **venv,
        "capability": dict(capability),
    }
