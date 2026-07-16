"""#2976 — the MCP stdio sandbox write scope.

A sandboxed stdio MCP server was granted write access to its ``cwd`` and nothing
else, so a launcher that bootstraps into a per-user cache (``npx`` → ``~/.npm``,
``uvx`` → ``~/.cache/uv`` + ``~/.local/share/uv``) was denied and never started.
These tests pin the three properties the fix rests on:

* the operator can DECLARE a write scope per server (the correctness mechanism —
  the per-runtime default map is an admittedly incomplete convenience);
* a ``~`` in any policy path is expanded by the backend (without this the grant
  lands on a literal ``<cwd>/~/...`` and the write stays denied — the failure
  mode that makes a wrong fix look right);
* widening the write scope must NOT re-open the sensitive-read deny-list. As of
  #2978 the deny rules are emitted AFTER the write grants (deny always wins), so
  even an overlapping grant no longer re-opens a credential path; the shipped
  defaults are nonetheless kept mechanically disjoint so no MCP server ever
  trips that narrowing (belt-and-suspenders).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from reyn.mcp.client import (
    _RUNTIME_DEFAULT_WRITE_PATHS,
    MCPClient,
    _looks_like_write_denial,
)
from reyn.security.sandbox.policy import (
    DEFAULT_SENSITIVE_READ_DENY,
    SandboxPolicy,
    expand_policy_path,
)


def _stdio(**extra) -> dict:
    return {"type": "stdio", "command": "npx", "args": ["-y", "pkg"], **extra}


# ── the operator-declared scope (the structural mechanism) ───────────────────


def test_operator_declared_write_paths_reach_the_policy():
    """Tier 2b: an operator's per-server `write_paths` reaches the sandbox policy.

    This is the property that makes an incomplete runtime-default map safe: an
    unknown runtime (or a relocated cache) degrades to one config line.
    """
    policy = MCPClient(
        _stdio(command="some-future-runtime", write_paths=["~/.future/cache"])
    )._build_mcp_sandbox_policy()

    assert "~/.future/cache" in policy.write_paths


def test_declared_write_paths_replace_the_runtime_defaults():
    """Tier 2b: declaring `write_paths` REPLACES the per-runtime defaults.

    The operator can narrow, not merely widen — narrowing is a security control,
    so a declared scope must not silently inherit a guessed grant.
    """
    policy = MCPClient(
        _stdio(command="npx", write_paths=["~/.custom-npm"])
    )._build_mcp_sandbox_policy()

    assert "~/.custom-npm" in policy.write_paths
    assert "~/.npm" not in policy.write_paths


def test_server_cwd_is_always_granted(tmp_path):
    """Tier 2b: the server's working dir survives an operator-declared scope.

    cwd is structural (the server's own workspace), not a per-runtime guess, so
    declaring `write_paths` must not drop it — the silent-drop class of #2964.
    """
    policy = MCPClient(
        _stdio(cwd=str(tmp_path), write_paths=[])
    )._build_mcp_sandbox_policy()

    assert str(tmp_path) in policy.write_paths


def test_unknown_runtime_gets_no_guessed_grant(tmp_path):
    """Tier 2b: an unrecognised runtime gets cwd only — no invented grant."""
    policy = MCPClient(
        _stdio(command="totally-unknown-launcher", cwd=str(tmp_path))
    )._build_mcp_sandbox_policy()

    assert policy.write_paths == [str(tmp_path)]


@pytest.mark.parametrize("command", sorted(_RUNTIME_DEFAULT_WRITE_PATHS))
def test_known_runtime_defaults_are_applied(command, tmp_path):
    """Tier 2b: a known launcher gets its measured cache grants with no config."""
    policy = MCPClient(
        _stdio(command=command, cwd=str(tmp_path))
    )._build_mcp_sandbox_policy()

    for expected in _RUNTIME_DEFAULT_WRITE_PATHS[command]:
        assert expected in policy.write_paths


def test_runtime_defaults_resolve_by_basename(tmp_path):
    """Tier 2b: an absolute launcher path resolves to the same runtime defaults.

    Operators write `command: /opt/homebrew/bin/npx` as readily as `npx`.
    """
    policy = MCPClient(
        _stdio(command="/opt/homebrew/bin/npx", cwd=str(tmp_path))
    )._build_mcp_sandbox_policy()

    assert "~/.npm" in policy.write_paths


# ── config contract ──────────────────────────────────────────────────────────


def test_write_paths_rejected_for_non_stdio_server():
    """Tier 1: `write_paths` on a non-stdio server is rejected, not ignored.

    It scopes a spawned subprocess, and only stdio spawns one. Silently ignoring
    it would read to the operator as a restriction that was never applied.
    """
    with pytest.raises(ValueError, match="write_paths"):
        MCPClient({"type": "http", "url": "https://x.test", "write_paths": ["~/.npm"]})


@pytest.mark.parametrize("bad", ["~/.npm", [1], {"p": "x"}])
def test_write_paths_must_be_a_list_of_strings(bad):
    """Tier 1: a malformed `write_paths` fails fast at construction."""
    with pytest.raises(ValueError, match="write_paths"):
        MCPClient(_stdio(write_paths=bad))


# ── the `~` expansion contract (the wrong-fix-looks-right failure mode) ──────


def test_expand_policy_path_expands_home():
    """Tier 1: `expand_policy_path` maps a leading `~` onto $HOME."""
    assert expand_policy_path("~/.npm") == Path.home() / ".npm"


def test_expand_policy_path_leaves_absolute_paths_alone():
    """Tier 1: a path without `~` is unchanged (no resolve(), no surprises)."""
    assert expand_policy_path("/var/data") == Path("/var/data")


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_tilde_write_grant_actually_permits_the_write(tmp_path):
    """Tier 2b: a `~`-relative write grant is ENFORCED as $HOME-relative.

    Behavioral, not a profile-format pin: an unexpanded `~` produces a grant for
    `<cwd>/~/...` and the real process is still denied, which is exactly how a
    plausible-looking fix fails. Runs the real backend against a real path under
    the user's home.
    """
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    # A real, home-relative target — the only kind that can catch the bug.
    target_dir = Path.home() / ".reyn_2976_write_probe"
    target_dir.mkdir(exist_ok=True)
    probe = target_dir / "probe"
    try:
        wrapped = backend.wrap_command(
            ["/usr/bin/touch", str(probe)],
            SandboxPolicy(write_paths=[f"~/{target_dir.name}"], allow_subprocess=True),
        )
        try:
            result = subprocess.run(wrapped.argv, capture_output=True, timeout=30)
        finally:
            wrapped.cleanup()

        assert result.returncode == 0, (
            "a `~`-relative write grant did not permit the write — the grant was "
            f"likely emitted for a literal '~' dir. stderr: {result.stderr!r}"
        )
        assert probe.exists()
    finally:
        probe.unlink(missing_ok=True)
        target_dir.rmdir()


# ── the deny-list must survive every write grant ─────────────────────────────


def test_shipped_runtime_defaults_are_disjoint_from_the_read_deny_list():
    """Tier 2b: no default write grant overlaps a sensitive-read deny path.

    Since #2978 the deny rules are emitted AFTER the write grants, so an overlap
    would be denied (deny wins) rather than nullifying the deny-list — but the
    shipped defaults are kept disjoint as belt-and-suspenders so no MCP server
    even triggers a `sandbox_policy_narrowed` narrowing. Any future entry that
    overlaps (`~`, `~/.config`, ...) must fail here.
    """
    denied = [expand_policy_path(d).resolve() for d in DEFAULT_SENSITIVE_READ_DENY]
    granted = {p for paths in _RUNTIME_DEFAULT_WRITE_PATHS.values() for p in paths}

    for raw in granted:
        grant = expand_policy_path(raw).resolve()
        for deny in denied:
            assert not (
                grant == deny
                or deny.is_relative_to(grant)
                or grant.is_relative_to(deny)
            ), f"default write grant {raw!r} overlaps sensitive-read deny path {deny}"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.parametrize("deny_path", list(DEFAULT_SENSITIVE_READ_DENY))
def test_default_mcp_policy_still_denies_credential_paths(deny_path):
    """Tier 2b: under the real MCP policy, credential paths stay unreadable.

    The end-to-end guard for the property above: widening the MCP write scope
    must not re-open `~/.ssh` & co. Exercised through the real builder and the
    real backend, because a policy object that looks right can still emit a
    profile that permits the read.
    """
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    target = expand_policy_path(deny_path)
    if not target.exists():
        pytest.skip(f"{deny_path} not present on this machine")

    # The probe must be one that SUCCEEDS when the read is permitted, otherwise
    # the assertion is vacuous: `cat` on a directory exits non-zero whatever the
    # sandbox decides, so it would "pass" against a policy that grants the read.
    # (Observed: with a deliberately-overlapping grant this test still passed for
    # every directory path and only caught the one regular file.)
    probe = ["/bin/cat"] if target.is_file() else ["/bin/ls"]

    policy = MCPClient(_stdio(command="npx"))._build_mcp_sandbox_policy()
    wrapped = backend.wrap_command([*probe, str(target)], policy)
    try:
        result = subprocess.run(wrapped.argv, capture_output=True, timeout=30)
    finally:
        wrapped.cleanup()

    assert result.returncode != 0, (
        f"the default MCP sandbox policy allowed a read of {deny_path} — a write "
        "grant re-opened the sensitive-read deny-list"
    )


# ── the operator-facing hint ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tail",
    [
        # Each observed verbatim from a real launcher denied by the real profile.
        "npm error code EPERM\nnpm error syscall open",
        "failed to open file `~/.cache/uv`: Operation not permitted (os error 1)",
        "[Errno 1] Operation not permitted: '/Users/x/.cache/probe'",
    ],
)
def test_write_denial_is_recognised_in_launcher_stderr(tail):
    """Tier 2b: a real launcher's denial stderr is recognised as a write denial.

    This is what points the operator at `write_paths` when the runtime default
    map does not cover their server.
    """
    assert _looks_like_write_denial(tail) is True


@pytest.mark.parametrize("tail", [None, "", "ModuleNotFoundError: no module named x"])
def test_unrelated_failures_are_not_flagged_as_write_denials(tail):
    """Tier 2b: an unrelated startup failure does not get the write-scope hint."""
    assert _looks_like_write_denial(tail) is False
