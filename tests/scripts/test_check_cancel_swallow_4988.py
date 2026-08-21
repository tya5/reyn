"""Tier 2: #4986/#4988 — the cancel-swallow gate itself.

7 of the 8 real sites #4988 fixed have no dedicated behavioral test (a
per-site test transcribing the same fix 8 times would be six-questions
②: the implementation, transcribed) — the gate IS the mechanism that
protects them. A "0 hits against the real tree" result alone cannot tell
"the pattern genuinely doesn't exist" apart from "the gate detects
nothing, ever" — this file is the fixture-based proof that the gate's
own detection actually fires, per this repo's own established
convention (mirrors ``tests/scripts/test_check_fastmcp_import_boundary_
3698.py``).

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files) — the function under test reads real file content and parses
real ASTs, so faking the filesystem would test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_cancel_swallow import offending_files


def test_cancel_then_await_then_swallow_with_no_cancelling_check_is_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: THE case #4986/#4988 exists for — a task this code owns is
    cancelled, awaited, and the resulting CancelledError is swallowed
    unconditionally, with no check for whether the CURRENT task was ALSO
    independently, externally cancelled at the same await."""
    (tmp_path / "worker.py").write_text(
        "import asyncio\n"
        "\n"
        "async def aclose(self):\n"
        "    self._task.cancel()\n"
        "    try:\n"
        "        await self._task\n"
        "    except asyncio.CancelledError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [(tmp_path / "worker.py", [(5, "self._task")])]


def test_the_fixed_shape_with_a_cancelling_check_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: accept-side — the ACTUAL fix #4988 applied at all 8 real
    sites (session.py's own #3377 precedent) must not itself be flagged;
    a gate that flags its own prescribed fix would be self-defeating."""
    (tmp_path / "worker.py").write_text(
        "import asyncio\n"
        "\n"
        "async def aclose(self):\n"
        "    self._task.cancel()\n"
        "    try:\n"
        "        await self._task\n"
        "    except asyncio.CancelledError:\n"
        "        current = asyncio.current_task()\n"
        "        if current is not None and current.cancelling() > 0:\n"
        "            raise\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_an_unconditional_reraise_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: a handler that re-raises unconditionally already
    propagates on every path — not a swallow, regardless of whether it
    also checks ``cancelling()``."""
    (tmp_path / "worker.py").write_text(
        "import asyncio\n"
        "\n"
        "async def aclose(self):\n"
        "    self._task.cancel()\n"
        "    try:\n"
        "        await self._task\n"
        "    except asyncio.CancelledError:\n"
        "        raise\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_awaiting_ones_own_body_without_a_preceding_cancel_is_not_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity — a coroutine's own ordinary try/except around
    ITS OWN body (no ``.cancel()`` call on the awaited name anywhere
    first) is a completely different, common, and correct shape (e.g.
    ``session.py``'s own ``run()`` loop swallowing its own cancellation
    to log and re-raise) — must not false-positive just because SOME
    name is awaited inside a CancelledError-catching try block."""
    (tmp_path / "session.py").write_text(
        "import asyncio\n"
        "\n"
        "async def run(self):\n"
        "    try:\n"
        "        await self.run_one_iteration()\n"
        "    except asyncio.CancelledError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_a_tuple_form_handler_is_also_flagged(tmp_path: Path) -> None:
    """Tier 2: #4988's own falsifier for the census's own miss — a grep
    for the literal single-exception-type line misses ``except
    (asyncio.CancelledError, SomeOtherError):`` (2 real sites,
    ``interfaces/web/server.py`` / ``mcp/subscription_port.py``, found
    only once the AST gate existed). This fixture pins that the AST
    match still catches the tuple form."""
    (tmp_path / "worker.py").write_text(
        "import asyncio\n"
        "\n"
        "class Lost(Exception):\n"
        "    pass\n"
        "\n"
        "async def aclose(self):\n"
        "    self._task.cancel()\n"
        "    try:\n"
        "        await self._task\n"
        "    except (asyncio.CancelledError, Lost):\n"
        "        pass\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [(tmp_path / "worker.py", [(8, "self._task")])]


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current tree (not assumed), matching the sibling gates' own
    "run it before shipping it" discipline. #4988 fixed all 8 real sites
    this gate's own census found; this asserts it stayed fixed."""
    from scripts.check_cancel_swallow import _SRC_DIR

    offenders = offending_files(_SRC_DIR)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
