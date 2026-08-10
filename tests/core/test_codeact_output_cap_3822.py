"""Tier 2: CodeAct reads its child's output through the capped reader (#3822)."""
from __future__ import annotations

import inspect

import pytest

from reyn.core.kernel import codeact_runner
from reyn.core.kernel.codeact_runner import CodeActRunner
from reyn.security.sandbox import _subprocess_io
from reyn.security.sandbox._subprocess_io import MAX_SUBPROCESS_OUTPUT_BYTES


def test_codeact_reads_through_the_capped_reader() -> None:
    """Tier 2: the seam that runs model-authored code has the same output cap
    every other command-level launch route has.

    ``_subprocess_io``'s own docstring is the argument: *"emitting unbounded
    output can OOM the host BEFORE the wall-clock timeout fires"*. CodeAct's
    timeout is therefore not a substitute — the cap exists precisely because the
    timeout arrives too late.

    The first assertion is a BINDING check, not a text match: it asks whether the
    name the runner will call resolves to the capped reader, which is a fact
    about the module rather than about how the call happens to be spelled. The
    second is a text match, and it is here only to catch a second, uncapped read
    being added alongside the first.

    **Limit, stated rather than papered over**: neither assertion runs a child.
    Proving the cap behaviourally means emitting more than the limit from a real
    snippet, which is slow and belongs to ``_subprocess_io``'s own tests — that
    module owns the cap and tests it. What is checked here is that CodeAct is on
    that path at all, which is the thing #3822 found it was not.
    """
    assert codeact_runner.communicate_capped is _subprocess_io.communicate_capped, (
        "the runner's `communicate_capped` is not the capped reader"
    )
    assert "proc.communicate(" not in inspect.getsource(codeact_runner), (
        "an uncapped `proc.communicate` read is back — plain communicate reads "
        "without a bound"
    )


@pytest.mark.asyncio
async def test_hitting_the_cap_is_reported_to_the_caller() -> None:
    """Tier 2: output past the cap sets ``truncated`` on the result.

    Driven by a REAL child emitting past the real limit — no faked reader, no
    lowered constant. The cap and the propagation are one behaviour from the
    caller's side, and a witness that patched either would be testing the patch.

    Why this exists as its own test: the cap swap and this propagation are
    separate changes that happen to ship together. Strip the swap and the other
    test goes red; strip only the propagation and, without this, everything
    stays green while the caller silently stops being told its output was cut —
    which is the #3688 shape the propagation was added to avoid.
    """
    async def dispatch(name: str, args: dict) -> dict:  # pragma: no cover - unused
        return {"status": "ok", "data": {}}

    over = MAX_SUBPROCESS_OUTPUT_BYTES + 1024
    code = f"print('x' * {over})\nresult = 'done'"
    out = await CodeActRunner().run(
        code=code, dispatch=dispatch, allow_unsandboxed=True, timeout=120.0,
    )

    assert out.get("truncated") is True, (
        f"output past the cap was not reported to the caller: "
        f"{ {k: v for k, v in out.items() if k != 'stdout'} }"
    )


@pytest.mark.asyncio
async def test_hitting_the_cap_is_reported_on_the_crash_path_too() -> None:
    """Tier 2: the SAME report on the no-final-frame path (#3821 follow-up).

    ``run`` returns through two exits and both propagate ``truncated``: the
    ``final_box`` frame above, and this one — the crash-path fallback taken when
    the snippet dies before the result channel carries a frame, where the reply
    is reconstructed from raw stdout/stderr.

    Why a second test rather than a second assertion: the first witness stripped
    BOTH propagation lines at once and went red, which proves at least one of
    them is covered — not both. A joint strip cannot distinguish "both witnessed"
    from "one witnessed and the other along for the ride", and here it was the
    latter: deleting this branch's line alone left the suite green. The cap is
    least likely to be a footnote exactly on the crash path, where the reader is
    already trying to work out what killed the snippet from its output.

    The child raises ``SystemExit`` after emitting past the cap. The harness
    surfaces every failure through ``except Exception``, which SystemExit is not
    a subclass of — so the interpreter unwinds with no final frame on the channel
    and the parent takes the reconstruct-from-stdout exit. The condition is
    produced by the child, not simulated at the seam, and needs no import (the
    snippet runs in safe mode, where ``os``/``sys`` are unavailable).
    """
    async def dispatch(name: str, args: dict) -> dict:  # pragma: no cover - unused
        return {"status": "ok", "data": {}}

    over = MAX_SUBPROCESS_OUTPUT_BYTES + 1024
    code = f"print('x' * {over}, flush=True)\nraise SystemExit(3)\n"
    out = await CodeActRunner().run(
        code=code, dispatch=dispatch, allow_unsandboxed=True, timeout=120.0,
    )

    # Which exit produced this reply, asserted rather than assumed: the
    # final-frame branch always attaches ``stdout``, so its absence is how this
    # test knows it is not quietly re-testing the branch above.
    assert "stdout" not in out, f"took the final-frame exit, not the crash path: {out.keys()}"
    assert out.get("truncated") is True, (
        f"the crash path dropped the truncation report: "
        f"{ {k: v for k, v in out.items() if k != 'stdout'} }"
    )
