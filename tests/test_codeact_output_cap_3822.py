"""Tier 2: CodeAct reads its child's output through the capped reader (#3822)."""
from __future__ import annotations

import inspect

from reyn.core.kernel import codeact_runner
from reyn.security.sandbox import _subprocess_io


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
