"""Tier 1/2: #6020 (architect census on #6017's own co-vet) — the post-kill
drain read that #5986/#6017 fixed in ``noop_backend.py``'s own 2 copies
existed byte-identically at 6 MORE sites (``container_backend.py`` ×2,
``landlock.py`` ×2, ``seatbelt.py`` ×2) — 8 total. Collapsed into ONE shared
helper, :func:`~reyn.security.sandbox._subprocess_io.drain_after_kill`.

lead-coder's own correction mid-review (architect re-derived their own
brief): a single BEHAVIOR witness cannot cover all 8 call sites — CI is
``ubuntu-latest`` everywhere, so ``seatbelt.py`` (Darwin-only) and
``landlock.py`` (Linux-only) never run their own edited code under CI at
all. This file therefore has TWO different kinds of test, matching the two
different things that need covering:

1. A real BEHAVIOR witness against the shared helper directly (this DOES
   run everywhere, since ``drain_after_kill`` itself has no platform gate —
   only the KILL mechanism upstream of it differs per backend, and this
   test drives the helper with a plain, portable ``subprocess.Popen`` +
   ``os.killpg``, not any one backend's own spawn path).
2. A STATIC census (no execution, no platform dependency) confirming each
   of the 4 backend files now DELEGATES to the helper — the old
   byte-identical ``except (asyncio.TimeoutError, Exception):`` copy is
   gone from all of them, and each contains the real number of
   ``drain_after_kill(`` calls the #6020 issue's own count expects. This
   is what actually protects ``seatbelt.py``/``landlock.py``'s own edits,
   since a CI-run behavior test structurally cannot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess

import pytest

from reyn.security.sandbox._subprocess_io import (
    communicate_capped,
    drain_after_kill,
    kill_process_tree,
)

_LOGGER = "reyn.security.sandbox._subprocess_io"

_BACKEND_FILES = {
    "src/reyn/security/sandbox/noop_backend.py": 2,
    "src/reyn/security/sandbox/backends/landlock.py": 2,
    "src/reyn/security/sandbox/backends/seatbelt.py": 2,
    "src/reyn/environment/container_backend.py": 2,
}


async def _wait_for_file(path) -> None:
    """Unbounded poll on a real filesystem condition — no attempt cap,
    matching ``test_subprocess_cancel_1470.py``'s own established helper;
    CI's own kill switch is the ceiling, not a chosen count."""
    while not path.exists():
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 1. Behavior witness — the shared helper itself, platform-agnostic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_drain_that_cannot_complete_in_time_warns_and_returns_empty(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: real subprocess, real kill, a real drain that genuinely
    never sees EOF (a detached grandchild survives the process-group
    kill — the SAME structure #6017's own noop-backend test used, now
    driving ``drain_after_kill`` directly instead of through one
    backend's ``run()``, since the property under test — does the
    helper narrow its except and warn — no longer depends on which
    backend calls it).

    Strip-falsifier (verified by hand: the helper's own narrowed except
    reverted to a bare ``except (asyncio.TimeoutError, Exception):``
    with no ``_logger.warning`` call): this test goes red — no
    ``WARNING`` record, and the #5990-class silent-except shape is
    back."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    ready = tmp_path / "ready"
    detached = tmp_path / "detached"
    # Same detach idiom as #6017's own test — `setsid`(1) isn't shipped
    # on macOS, so `os.setsid()` runs directly inside the backgrounded
    # python3 process, which touches its OWN ready marker (an observable
    # condition — killing before this lands would leave the grandchild
    # in the ORIGINAL group, killed along with it) then execs into
    # `sleep 60`, keeping the inherited stdout write end open past the
    # kill.
    script = (
        f"touch {ready}\n"
        "(python3 -c \"import os; os.setsid(); "
        f"open('{detached}', 'w').write(str(os.getpid())); "
        "os.execvp('sleep', ['sleep', '60'])\" &)\n"
        "sleep 60\n"
    )
    proc = subprocess.Popen(
        ["/bin/sh", "-c", script], start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    comm_future = loop.run_in_executor(
        None, lambda: communicate_capped(proc, timeout=30.0),
    )
    try:
        await _wait_for_file(ready)
        await _wait_for_file(detached)
        await kill_process_tree(proc)

        stdout_b, stderr_b, truncated = await drain_after_kill(
            comm_future, grace_seconds=0.2, context="test",
        )

        assert (stdout_b, stderr_b, truncated) == (b"", b"", False), (
            "the detached grandchild keeps the pipe open past the drain "
            "grace -- empty output is the correct, honest result here"
        )
        drain_warnings = [
            r.getMessage() for r in caplog.records
            if r.name == _LOGGER and r.levelname == "WARNING"
            and "could not capture partial output" in r.getMessage()
        ]
        assert drain_warnings != [], (
            f"#6020 REGRESSION: a drain that could not complete in time "
            f"must be logged, not silently swapped for empty output — got "
            f"{[(r.name, r.getMessage()) for r in caplog.records]!r}"
        )
        assert "test" in drain_warnings[0], (
            "the context string must actually reach the log line — "
            f"got {drain_warnings[0]!r}"
        )
    finally:
        grandchild_pid = int(detached.read_text())
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_a_normal_kill_with_time_to_drain_needs_no_warning(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: regression guard — an ORDINARY kill (no detached grandchild
    holding the pipe) drains within grace and stays exactly as quiet as
    before; the fix adds a signal on the failure path, not noise on the
    routine one."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    ready = tmp_path / "ready"
    script = f"touch {ready}\nsleep 60\n"
    proc = subprocess.Popen(
        ["/bin/sh", "-c", script], start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    comm_future = loop.run_in_executor(
        None, lambda: communicate_capped(proc, timeout=30.0),
    )
    await _wait_for_file(ready)
    await kill_process_tree(proc)

    stdout_b, stderr_b, truncated = await drain_after_kill(
        comm_future, grace_seconds=3.0, context="test",
    )

    assert truncated is False
    drain_warnings = [
        r.getMessage() for r in caplog.records
        if r.name == _LOGGER and r.levelname == "WARNING"
        and "could not capture partial output" in r.getMessage()
    ]
    assert drain_warnings == [], (
        f"#6020 REGRESSION: a drain that completed normally must not warn "
        f"— got {drain_warnings!r}"
    )


# ---------------------------------------------------------------------------
# 2. Static census — the delegation itself, platform-agnostic (no execution)
# ---------------------------------------------------------------------------


def test_the_unnarrowed_drain_except_pattern_is_absent_from_the_backend_files() -> None:
    """Tier 1: the property #6020's own issue body names as the accept
    criterion (`git grep -c 'except \\((asyncio\\.)?TimeoutError,
    ?Exception\\)' -- src` == 0) — enforced as a real, running gate
    rather than a one-time manual check, so a FUTURE 9th copy (the exact
    failure mode #6020 exists to prevent) is caught structurally, not by
    someone remembering to grep by hand.

    ``_subprocess_io.py`` itself is excluded on purpose: its OWN
    remaining instance (inside ``kill_process_tree``'s ``_wait_grace``)
    is a DIFFERENT operation — waits for process EXIT, never returns
    output, and a broad catch there is a defensible fail-safe (escalate
    to SIGKILL on any doubt), not the silent-data-loss shape the 8
    drain-read copies had. Reviewed and left in place, with its own
    comment explaining why (see that method's own docstring)."""
    import re

    pattern = re.compile(r"except \((asyncio\.)?TimeoutError, ?Exception\)")
    repo_root = _repo_root()
    for rel_path in _BACKEND_FILES:
        text = (repo_root / rel_path).read_text()
        matches = pattern.findall(text)
        assert matches == [], (
            f"#6020 REGRESSION: {rel_path} still has the old, unnarrowed "
            f"except — {len(matches)} occurrence(s), should be 0 (must "
            f"delegate to drain_after_kill instead)"
        )


def test_every_backend_file_delegates_to_the_shared_helper_the_right_number_of_times() -> None:
    """Tier 1: the OTHER half of the census — narrowing the except away
    is necessary but not sufficient; each backend must actually CALL
    ``drain_after_kill`` at every one of its own former inline-copy
    sites (2 each, per #6020's own issue-body count), not merely have
    deleted the old code with nothing calling the new one."""
    repo_root = _repo_root()
    for rel_path, expected_calls in _BACKEND_FILES.items():
        text = (repo_root / rel_path).read_text()
        actual_calls = text.count("drain_after_kill(")
        assert actual_calls == expected_calls, (
            f"#6020 REGRESSION: {rel_path} calls drain_after_kill( "
            f"{actual_calls} time(s), expected {expected_calls} — either "
            f"a call site was missed or an extra one was added"
        )


def _repo_root():
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate repo root from " + str(here))
