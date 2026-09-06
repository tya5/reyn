"""Tier 2: #5840 -- MCPClient.initialize()'s own permission-failure hint
states what reyn granted, never a causal claim about the sandbox.

Owner-hit, silent: the pre-fix hint read `_looks_like_write_denial(tail):
hint += "the sandbox DENIED a write ..."` -- a stderr STRING MATCH promoted
to a CAUSAL assertion. `PermissionError: [Errno 1] Operation not permitted:
'<path>'` is IDENTICAL whether the sandbox denied a write outside its
granted scope OR a genuine non-sandbox permission failure occurred INSIDE
that scope (a read-only mount, a missing parent directory, a real
permissions problem) -- the exact entailment gap `security/sandbox/
denial.py`'s own module comment already names (#5832/#5839, merged the
same night): "a write denial has no such disjoint signature ... 'EPERM
means the sandbox did it' would be the exact bandaid."

Reused, not re-derived: `looks_permission_related` (the SAME #5832 gate
`hooks/shell_runner.py` already uses) decides whether disclosing what reyn
granted is worth the sentence; it never classifies a cause. The narrower,
Seatbelt-observed `_looks_like_write_denial` markers still gate the
SPECIFIC write_paths remedy suggestion (unchanged) -- offered
conditionally ("if this IS the cause"), never asserted.

Real subprocess, real OS-level PermissionError (`chmod 0` on a real
directory, then a real `open()` inside it) -- no mock, no fake backend --
mirrors `tests/hooks/test_1800_hook_shell_runner.py`'s own established
`test_permission_failure_discloses_granted_sandbox_range` control shape.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient, MCPError

# #5028's own ratchet (`scripts/check_subprocess_reyn_pin.py`): every
# tests/ file spawning `sys.executable` declares `out_of_process_reyn`,
# with no "this spawn never imports reyn" carve-out -- a grandfathered
# baseline is for files that predate the gate, never a place to add a new
# one (lead-coder, #5846 review). None of this file's 3 spawns actually
# import `reyn` today, but the declaration is what keeps that true under
# a future edit, not an assumption this file gets to make on its own.
_PIN_ENV = lambda out_of_process_reyn: {  # noqa: E731
    "PATH": os.environ.get("PATH", ""),
    "PYTHONPATH": out_of_process_reyn,
}


def _client_that_hits_a_real_permission_error(
    locked_dir: Path, out_of_process_reyn: str
) -> MCPClient:
    """A real stdio MCPClient whose subprocess tries to ``open()`` a file
    inside *locked_dir* (chmod 0o000, a REAL OS-level denial -- nothing to
    do with reyn's own sandbox, which this client never even wraps the
    command through here: the default `subprocess=True`/no sandbox backend
    posture is what a plain MCPClient() construction gets)."""
    target = locked_dir / "cursor.json.tmp"
    return MCPClient({
        "type": "stdio",
        "command": sys.executable,
        "args": ["-c", f"open({str(target)!r}, 'w')"],
        "env": _PIN_ENV(out_of_process_reyn),
    })


@pytest.mark.asyncio
async def test_real_permission_failure_discloses_the_granted_range_not_a_verdict(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: the #5840 witness -- a REAL, non-sandbox-caused PermissionError
    (chmod 0, no sandbox enforcement anywhere in this path) still gets the
    subprocess/network/write_paths disclosure (the FACT reyn can honestly
    state), but the hint must NEVER claim the sandbox denied anything --
    it did not; this is an ordinary filesystem permission error reyn cannot
    attribute."""
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o000)
    client = _client_that_hits_a_real_permission_error(locked_dir, out_of_process_reyn)
    try:
        with pytest.raises(MCPError) as excinfo:
            await client.initialize()
    finally:
        locked_dir.chmod(0o755)  # restore so tmp_path cleanup can remove it

    msg = str(excinfo.value)
    assert "operation not permitted" in msg.lower() or "permission" in msg.lower(), (
        f"the real PermissionError text did not reach the raised MCPError: {msg!r}"
    )
    assert "#5840" in msg and "write_paths=" in msg and "network=" in msg, (
        f"expected the granted-range disclosure (subprocess/network/write_paths) "
        f"on this failure -- got {msg!r}"
    )
    assert "sandbox denied" not in msg.lower() and "the sandbox denied" not in msg.lower(), (
        f"the hint must never claim the sandbox caused a failure it cannot "
        f"actually attribute -- got {msg!r}"
    )


@pytest.mark.asyncio
async def test_write_shaped_epermreal_subprocess_still_offers_the_remedy(
    out_of_process_reyn: str,
) -> None:
    """Tier 2: acceptance ② (remedy usefulness is not lost) -- a real
    subprocess that produces the write-denial-SHAPED marker
    (`_looks_like_write_denial`'s own EPERM/"operation not permitted"
    signature -- #2976's own module comment: this is the SEATBELT shape,
    observed from a real sandboxed launch, e.g. npm/uv/Python's own
    "[Errno 1] Operation not permitted") still gets the conditionally-
    worded write_paths remedy alongside the disclosure -- an operator who
    reads "Operation not permitted" still gets pointed at the one config
    knob that would fix it, IF that turns out to be the real cause.

    A real, unsandboxed `chmod 0` write denial on this machine produces
    EACCES ("Permission denied"), not EPERM (confirmed directly, see the
    sibling non-write-shaped test below) -- EPERM specifically is the
    SANDBOX's own denial shape, per #2976's own comment, not an ordinary
    filesystem permission error's. So this test drives a real subprocess
    printing that exact, previously-CAPTURED (#2976's own module comment)
    marker text -- the same unit-level shape `test_write_denial_is_
    recognised_in_launcher_stderr` (test_2976_mcp_sandbox_write_paths.py)
    already establishes for `_looks_like_write_denial` itself."""
    client = MCPClient({
        "type": "stdio",
        "command": sys.executable,
        "args": [
            "-c",
            "import sys; sys.stderr.write("
            "\"PermissionError: [Errno 1] Operation not permitted: "
            "'/tmp/some/path'\\n\"); sys.exit(1)",
        ],
        "env": _PIN_ENV(out_of_process_reyn),
    })
    with pytest.raises(MCPError) as excinfo:
        await client.initialize()

    msg = str(excinfo.value)
    assert "write_paths" in msg and "IS the cause" in msg, (
        f"expected the conditionally-worded write_paths remedy for a "
        f"write-shaped EPERM -- got {msg!r}"
    )
    assert "sandbox denied" not in msg.lower()


@pytest.mark.asyncio
async def test_a_non_write_shaped_permission_failure_gets_no_write_paths_remedy(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: acceptance ② the other direction -- a REAL, non-sandboxed
    permission failure (chmod 0, a genuine OS-level EACCES -- confirmed by
    the previous test's own docstring measurement, NOT the sandbox-shaped
    EPERM) matches `looks_permission_related`'s broader "permission
    denied"/EACCES markers but not `_looks_like_write_denial`'s narrower
    EPERM/"operation not permitted" ones -- so it still gets the
    granted-range disclosure, but NOT the write-specific remedy:
    suggesting `write_paths` for a failure that was never write-shaped
    would be a wrong-knob remedy, not merely an unproven cause."""
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o000)
    client = _client_that_hits_a_real_permission_error(locked_dir, out_of_process_reyn)
    try:
        with pytest.raises(MCPError) as excinfo:
            await client.initialize()
    finally:
        locked_dir.chmod(0o755)

    msg = str(excinfo.value)
    assert "errno 13" in msg.lower() or "permission denied" in msg.lower(), (
        f"expected a real EACCES (not EPERM) from the chmod-0 denial -- "
        f"got {msg!r} (if this now reads EPERM, this test's own "
        f"OS-behavior assumption needs re-measuring, not silently patching)"
    )
    assert "write_paths=" in msg and "network=" in msg, (
        f"the granted-range disclosure must still fire for a permission-"
        f"related (if not write-shaped) failure -- got {msg!r}"
    )
    assert "IS the cause" not in msg and "write_paths: [" not in msg, (
        f"the write-specific remedy suggestion must NOT appear for a "
        f"non-write-shaped permission failure -- got {msg!r}"
    )
    assert "sandbox denied" not in msg.lower()


@pytest.mark.asyncio
async def test_an_unrelated_failure_gets_no_permission_hint_at_all() -> None:
    """Tier 2: control arm -- an ordinary, non-permission-shaped subprocess
    failure (e.g. a Python ImportError) triggers neither the disclosure
    nor the remedy; the #5840 hint is not noise on every unrelated
    failure."""
    client = MCPClient({
        "type": "stdio",
        "command": sys.executable,
        "args": ["-c", "import definitely_not_a_real_module_xyz"],
    })
    with pytest.raises(MCPError) as excinfo:
        await client.initialize()

    msg = str(excinfo.value)
    assert "#5840" not in msg, (
        f"the permission-disclosure hint fired on an unrelated failure: {msg!r}"
    )
