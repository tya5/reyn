"""#3705 — closure: every Session-owned `.reyn` write path is anchored on an
explicit workspace root when the caller supplies one, and a repo-wide AST
sweep enumerates every remaining cwd-relative `.reyn` literal so none of
them can silently reappear (or silently spread) without this test noticing.

Companion to `tests/runtime/test_session_writes_stay_in_its_workspace_3705.py`
(the falsifiable, end-to-end RED gate a session's `history.jsonl` write must
pass). This file covers the OTHER Session-owned sites the incident's
full-repo sweep found (`agent.py`'s `workspace_dir`, `recovery.py`'s
`default_snapshot_path`, `memory_service.py`'s "shared" layer,
`router_host_adapter.py`'s state dir + shared memory path) plus the sweep's
own closure.

Real objects throughout — real `Agent`/`Session`/`MemoryService`/
`RouterHostAdapter` construction, real filesystem `tmp_path` roots. No
mocks.
"""
from __future__ import annotations

import ast
from pathlib import Path

from reyn.runtime.agent import Agent
from reyn.runtime.services.recovery import default_snapshot_path
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT
_SRC_ROOT = _REPO_ROOT / "src"

# ---------------------------------------------------------------------------
# Site-level fixes — each Session-owned location the #3705 sweep found
# ---------------------------------------------------------------------------


def test_agent_workspace_dir_respects_an_explicit_workspace_state_dir(tmp_path):
    """Tier 2: the fulcrum fix — `Agent.workspace_dir` used to be a bare
    relative literal, ignoring `workspace_state_dir` even when the caller
    explicitly supplied one. This is what let 68 test-fixture agent
    directories land in the owner's real `.reyn/agents/`."""
    root = tmp_path / "isolated-project" / ".reyn"
    agent = Agent(agent_name="alpha", workspace_state_dir=root)

    assert agent.workspace_dir == root / "agents" / "alpha"


def test_agent_workspace_dir_falls_back_to_cwd_when_unset(tmp_path, monkeypatch):
    """Tier 2: regression guard — a caller that never sets
    `workspace_state_dir` keeps the EXACT prior default (cwd-relative), so
    #3705's fix changes nothing for callers that don't opt in."""
    monkeypatch.chdir(tmp_path)
    agent = Agent(agent_name="alpha")

    assert agent.workspace_dir == tmp_path / ".reyn" / "agents" / "alpha"


def test_default_snapshot_path_respects_an_explicit_root(tmp_path):
    """Tier 2: `recovery.default_snapshot_path`'s new `root=` param."""
    root = tmp_path / ".reyn"
    path = default_snapshot_path("alpha", root=root)

    assert path == root / "agents" / "alpha" / "state" / "snapshot.json"


def test_default_snapshot_path_falls_back_to_cwd_when_root_is_none(tmp_path, monkeypatch):
    """Tier 2: regression guard — `root=None` (the default) is byte-identical
    to the pre-#3705 behavior."""
    monkeypatch.chdir(tmp_path)
    path = default_snapshot_path("alpha")

    assert path == tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"


# ---------------------------------------------------------------------------
# BOTH make_session test helpers — lead-coder review (#3714): there are TWO
# ("tests/_support/session.py" and "tests/_support/agent_session.py", the
# latter used across 223 files) and the RED gate only exercises the first.
# `agent_session.make_session` forwards `workspace_state_dir` only when the
# CALLER passes it (unlike the other helper, which now sets one by
# default) — and was still calling `default_snapshot_path(agent.agent_name)`
# with no `root=`, silently dropping it even when a caller did pass one.
# Falsified below through BOTH helpers so neither can regress unnoticed.
# ---------------------------------------------------------------------------


def test_agent_session_make_session_threads_root_into_default_snapshot_path(
    tmp_path, monkeypatch,
):
    """Tier 2: `tests/_support/agent_session.make_session` — the OTHER
    `make_session` helper (223 test files use it, not the one the original
    RED gate happens to import) — must pass its caller's `workspace_state_dir`
    through to `default_snapshot_path`'s `root=` param (added by #3705).

    Note this is NOT the same bug the history.jsonl RED gate catches:
    `Session.__init__`'s OWN `default_snapshot_path` call (already fixed,
    reading `self._reyn_state_root`) independently produces a correct
    `session._snapshot_path` regardless of this helper's local variable — so
    an end-to-end "did history.jsonl land in the right place" check for
    THIS helper stays green even with the bug (verified: it does). The
    actual observable defect is narrower: this helper's own local
    `snapshot_path` (computed BEFORE `Session` exists, to build
    `generation_store`/`journal` via `build_recovery` — see
    `services/recovery.py`) was silently anchored on cwd regardless of
    `workspace_state_dir`, which would put the PITR generation store under
    the wrong directory. Witnessed directly via a call-through spy on
    `default_snapshot_path`, asserting the actual `root=` it was called
    with — the precise seam the bug lived in."""
    import tests._support.agent_session as agent_session_mod

    calls: "list[Path | None]" = []
    orig = agent_session_mod.default_snapshot_path

    def _spy(agent_name, root=None):
        calls.append(root)
        return orig(agent_name, root=root)

    monkeypatch.setattr(agent_session_mod, "default_snapshot_path", _spy)

    root = tmp_path / ".reyn"
    agent_session_mod.make_session(agent_name="alpha", workspace_state_dir=root)

    assert calls == [root], (
        f"default_snapshot_path must be called with root={root!r} when the "
        f"caller supplied workspace_state_dir; got {calls}"
    )


# ---------------------------------------------------------------------------
# Closure — AST sweep of every cwd-relative `.reyn` literal left in src/
# ---------------------------------------------------------------------------


def _is_reyn_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == ".reyn"


def _is_bare_reyn_path_call(node: ast.AST) -> bool:
    """``Path(".reyn")`` — a bare relative literal, always cwd-anchored."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Path"
        and len(node.args) == 1
        and _is_reyn_literal(node.args[0])
    )


def _is_cwd_reyn_binop(node: ast.AST) -> bool:
    """``Path.cwd() / ".reyn"`` — explicit but still cwd-anchored."""
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
        return False
    left = node.left
    is_cwd_call = (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Attribute)
        and left.func.attr == "cwd"
    )
    return is_cwd_call and _is_reyn_literal(node.right)


def _cwd_relative_reyn_sites(py_file: Path) -> "list[int]":
    """Line numbers of every AST-level ``Path(".reyn")`` /
    ``Path.cwd() / ".reyn"`` expression in ``py_file`` — never a
    string-grep, so a comment or docstring MENTIONING the pattern (as this
    very file's own docstrings do, explaining the fix) is never a false
    positive."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    lines: "list[int]" = []
    for node in ast.walk(tree):
        if _is_bare_reyn_path_call(node) or _is_cwd_reyn_binop(node):
            lines.append(node.lineno)
    return lines


# Every remaining cwd-relative `.reyn` site, as of #3705's fix, with why:
#
# - Session-owned FALLBACKS (only reached when a caller does NOT supply an
#   explicit workspace root — preserves the pre-#3705 default for callers
#   that never had one to give, per the site-level tests above):
#   agent.py, recovery.py, session.py (`_reyn_state_root`),
#   router_host_adapter.py, replay_preconditions.py (explicitly documented
#   as intentionally mirroring router_host_adapter's own convention).
#   #3716: `data/memory/memory_paths.py`'s `memory_dir(root=None)` fallback
#   joins this group too — `reyn memory` now HAS project-root plumbing
#   (`interfaces/cli/commands/memory.py`'s `_project_reyn_root`), so this
#   ONE site (a single `base = root if root is not None else Path.cwd() /
#   ".reyn"` shared by both the "shared" and "agent" branches) is the
#   intentional `root=None` default, not an unaddressed gap
#   (lead-coder review, #3722: the gate counts sites, it does not read
#   REASONS — a stable count at an already-allowlisted path can silently go
#   stale in what it's claiming, which is exactly what happened here when
#   #3716 landed without also updating this comment).
# - Genuine top-level CLI entry points, where "operate on whatever project
#   the operator is standing in" IS the intended behavior (the same way
#   `git status` reads the cwd) — not Session-owned, not reachable via
#   `make_session`, and not implicated in the incident (a test never
#   constructs `reyn agent list` / `reyn events` / etc. the way it
#   constructs a `Session`):
#   interfaces/cli/commands/mcp.py,
#   interfaces/web/server.py (the `reyn web` server's own persist paths),
#   dev/dogfood/runner.py.
#   (#4204 bucket A: `agent.py`, `dogfood.py`, `events.py`, and `web.py`
#   moved OFF this list — they now anchor on
#   `_find_project_root(Path.cwd()) or Path.cwd()` like the other CLI
#   command modules that already made this switch, rather than a bare
#   cwd-relative literal, so none of the four appears in the sweep
#   below.)
# - Deferred (filed separately, NOT fixed here): `interfaces/slash/memory.py`
#   (#3721) — the `/memory` chat command calls `list_entries()`/`find_one()`
#   with no arguments, falling into the same `memory_dir()` cwd-relative
#   default, but this file has ZERO `Path(".reyn")`/`Path.cwd() / ".reyn"`
#   literals of its OWN (the defect lives entirely in the CALLEE it doesn't
#   override), so it does not appear as its own allowlist entry — #3721 is
#   tracked there, not here, because this gate only sees literal cwd-anchor
#   EXPRESSIONS, not "a caller that omitted an available override".
_ALLOWED_SITES: "dict[str, int]" = {
    "src/reyn/runtime/agent.py": 1,
    "src/reyn/runtime/services/recovery.py": 1,
    "src/reyn/runtime/session.py": 1,
    "src/reyn/runtime/services/router_host_adapter.py": 1,
    "src/reyn/dev/testing/replay_preconditions.py": 1,
    "src/reyn/data/memory/memory_paths.py": 1,  # Session-owned fallback (root=None), see comment above
    "src/reyn/interfaces/cli/commands/mcp.py": 2,
    "src/reyn/interfaces/web/server.py": 2,
    "src/reyn/dev/dogfood/runner.py": 1,
}


def test_every_remaining_cwd_relative_reyn_site_is_accounted_for():
    """Tier 2: #3705 closure — a full-repo AST sweep of `src/` for
    `Path(".reyn")` / `Path.cwd() / ".reyn"` must find EXACTLY the allowlisted
    sites above, each with a stated reason (Session-owned fallback / genuine
    CLI entrypoint / explicitly deferred). A NEW site appearing anywhere
    else means either a regression (a #3705-fixed location reverted to the
    ambient-cwd form) or a fresh instance of the SAME bug class introduced
    elsewhere — both must be caught here, not rediscovered the way the
    original incident was."""
    found: "dict[str, int]" = {}
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        sites = _cwd_relative_reyn_sites(py_file)
        if sites:
            found[str(py_file.relative_to(_REPO_ROOT))] = len(sites)

    # #3714 review (lead-coder): the original two-sided check compared
    # `unexpected` (new PATHS) and `missing` (count DECREASES) separately —
    # an allowlisted path's count going UP (a 4th site appearing in an
    # already-allowlisted file) satisfied neither check and stayed green.
    # A single set-of-mismatches comparison, over the UNION of both key
    # sets, catches every direction: a brand-new path (found, not
    # allowlisted), a vanished path (allowlisted, not found), AND a changed
    # count at an already-allowlisted path — each is exactly "found's count
    # != allowed's count" (treating an absent key as count 0).
    all_paths = set(found) | set(_ALLOWED_SITES)
    mismatches = {
        path: {"found": found.get(path, 0), "allowed": _ALLOWED_SITES.get(path, 0)}
        for path in all_paths
        if found.get(path, 0) != _ALLOWED_SITES.get(path, 0)
    }
    assert not mismatches, (
        f"cwd-relative `.reyn` site count(s) don't match the allowlist exactly: "
        f"{mismatches} — a brand-new site needs a reason added to the allowlist, "
        f"a fixed site needs its allowlist entry removed/reduced, and a count "
        f"increase at an already-allowlisted path needs its own review (it is "
        f"NOT automatically covered by that path already being allowlisted)"
    )
