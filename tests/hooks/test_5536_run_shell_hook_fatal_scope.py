"""Tier 2: #5536 group C — ``run_shell_hook``'s own outer catch-all
(``shell_runner.py``'s last ``except Exception``) no longer swallows a bug
in reyn's own code.

architect ruling (#5536): this site is NOT a silence question — it already
``_log.error``s. It is a SCOPE question: ``except Exception`` also catches
an ``AttributeError``/``TypeError``/``KeyError`` raised by reyn's OWN
logic inside this function, giving it the exact same "the hook run
failed, return None" outcome an ordinary external command failure gets —
hiding a genuine reyn defect behind an indistinguishable-looking hook
failure. Processed by reusing the CLOSED allowlist ``classify_llm_
failure``'s own FATAL branch already uses (``reyn.services.compaction.
engine.FATAL_EXC_TYPES`` — made public for this reuse, #3783 §2's own
"An AttributeError in our own code must not become [silently absorbed]"
reasoning applied at a second call site, no new concept introduced).

Accept-side pair:
① a FATAL-classified exception (AttributeError) raised from inside the
  try block now PROPAGATES OUT of run_shell_hook, instead of being
  logged-and-swallowed into a ``None`` return.
② deny/regression — an ORDINARY exception (OSError — the shape a real
  external I/O failure raises) is still caught exactly as before:
  logged at ERROR, ``None`` returned, never propagates. Without this,
  ① alone could pass under an implementation that stopped catching
  ANYTHING here, which would break every existing caller relying on
  run_shell_hook's own documented "never raises, fail-safe" contract
  for ordinary failures.

Both drive the REAL ``run_shell_hook`` with a real (non-Mock)
``SandboxBackend``-shaped object whose own ``run()`` raises the exception
under test — the same "real callable that raises" pattern this session's
own #5536 group A/B files already use (``_raising_emit``), applied to the
sandbox seam instead.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from reyn.security.sandbox import SandboxPolicy


class _RaisingBackend:
    """A real (non-Mock) SandboxBackend whose own ``run()`` raises
    *exc_factory()* — simulates a bug reached from inside run_shell_hook's
    own try block, at the seam furthest from this function's own
    top-level logic (proving the exception genuinely traverses the whole
    function, not just a line near the top)."""

    name = "raising"

    def __init__(self, exc_factory) -> None:
        self._exc_factory = exc_factory

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None, hook_process_context=None):
        raise self._exc_factory()


@pytest.mark.asyncio
async def test_a_fatal_bug_exception_propagates_instead_of_being_swallowed(
    tmp_path: Path, caplog, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: ① — AttributeError (one of the 3 FATAL_EXC_TYPES) raised
    from inside run_shell_hook's own try block propagates out, rather
    than being logged-and-absorbed into a ``None`` return.

    Strip-falsify: remove the ``if isinstance(exc, FATAL_EXC_TYPES): raise``
    branch in the outer except (revert to the pre-#5536 bare catch) and
    this test goes RED — ``run_shell_hook`` returns ``None`` instead of
    raising (performed during review)."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RaisingBackend(lambda: AttributeError("'NoneType' object has no attribute 'x'"))

    with caplog.at_level(logging.ERROR), pytest.raises(AttributeError):
        await run_shell_hook(
            ["true"],
            event_context={"events": [{"event": "turn_end"}], "skipped_session_wide": 0},
            timeout_seconds=10,
            sandbox_backend=backend,
            sandbox_policy=SandboxPolicy(network=False, deny_subprocess=True, timeout_seconds=10),
            allowlist_path=tmp_path / "allowlist.json",
        )

    # never absorbed into the ordinary "unexpected error" log line this
    # site emits for a genuinely swallowed exception — the FATAL branch
    # re-raises before that log call is reached.
    assert not any("unexpected error" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_an_ordinary_exception_is_still_caught_and_logged(
    tmp_path: Path, caplog, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: ② — deny/regression side. An OSError (NOT in
    FATAL_EXC_TYPES — the shape a real external I/O failure raises, e.g.
    the sandbox subprocess machinery itself failing) is caught exactly
    as before: logged at ERROR, ``None`` returned, never propagates.
    Without this, ① could pass under a broken implementation that
    stopped catching anything at all here.

    The property under test is "an ERROR-level log fires and the call
    returns None" — NOT the exact log wording (lead-coder review note,
    #5536 A/B: pin the property, not the phrasing).

    Strip-falsify: widen the FATAL_EXC_TYPES check to re-raise
    unconditionally (``if True: raise``) and this test goes RED — the
    OSError propagates out of run_shell_hook instead of being caught,
    an unhandled exception in this test's own await (performed during
    review)."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RaisingBackend(lambda: OSError("subprocess machinery failed"))

    with caplog.at_level(logging.ERROR):
        result = await run_shell_hook(
            ["true"],
            event_context={"events": [{"event": "turn_end"}], "skipped_session_wide": 0},
            timeout_seconds=10,
            sandbox_backend=backend,
            sandbox_policy=SandboxPolicy(network=False, deny_subprocess=True, timeout_seconds=10),
            allowlist_path=tmp_path / "allowlist.json",
        )

    assert result is None
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    (only,) = error_records  # exactly one ERROR log — unpack-must-flip
    assert "true" in only.getMessage() or "OSError" in only.getMessage()
