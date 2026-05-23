"""Shim re-export — canonical module is :mod:`reyn.safe.time`.

FP-0042 backward-compat: import from reyn.api.safe.time is
forwarded to the canonical reyn.safe.time. Scheduled for removal
in the next release — please migrate to from reyn.safe import time.
"""
from reyn.safe.time import *  # noqa: F401,F403 — re-export
