"""Shim re-export — canonical module is :mod:`reyn.safe.hash`.

FP-0042 backward-compat: import from reyn.api.safe.hash is
forwarded to the canonical reyn.safe.hash. Scheduled for removal
in the next release — please migrate to from reyn.safe import hash.
"""
from reyn.safe.hash import *  # noqa: F401,F403 — re-export
