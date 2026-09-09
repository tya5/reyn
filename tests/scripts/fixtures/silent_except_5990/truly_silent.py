"""Fixture for silent_except_ratchet.py's own test suite (#5990).

Not a real module — never imported, never collected as a test. This IS
the witness that the detector's own "silent" branch fires on a genuinely
silent except: no log call above debug, no re-raise, no other visible
marker. `tests/scripts/test_silent_except_ratchet_5990.py` reads this
file directly by path (parsed, never executed).
"""
import logging

logger = logging.getLogger(__name__)


def risky_call() -> None: ...
def another_risky_call() -> None: ...


def do_something_risky() -> None:
    try:
        risky_call()
    except Exception:
        pass


def do_something_else_risky() -> None:
    try:
        another_risky_call()
    except Exception as exc:
        logger.debug("swallowed: %s", exc)
