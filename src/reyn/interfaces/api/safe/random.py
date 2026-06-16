"""Shim re-export — canonical module is :mod:`reyn.safe.random`.

FP-0042 backward-compat: import from reyn.interfaces.api.safe.random is
forwarded to the canonical reyn.safe.random. Scheduled for removal
in the next release — please migrate to from reyn.safe import random.
"""
from reyn.safe.random import *  # noqa: F401,F403 — re-export
