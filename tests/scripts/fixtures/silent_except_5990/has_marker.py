"""Fixture for silent_except_ratchet.py's own test suite (#5990).

Not a real module — never imported, never collected as a test. This IS
the witness that the detector does NOT flag an except block that already
carries a visible marker — the false-reject side of the discriminator,
required alongside `truly_silent.py`'s false-accept-side witness so a
detector that simply flags every except (structurally unable to ever
say "fine") cannot pass this suite.
`tests/scripts/test_silent_except_ratchet_5990.py` reads this file
directly by path (parsed, never executed).
"""
import logging

logger = logging.getLogger(__name__)


def risky_call() -> None: ...
def another_risky_call() -> None: ...


def do_something_risky() -> None:
    try:
        risky_call()
    except Exception as exc:
        logger.error("risky_call failed: %s", exc)


def do_something_else_risky() -> None:
    try:
        another_risky_call()
    except Exception:
        raise
