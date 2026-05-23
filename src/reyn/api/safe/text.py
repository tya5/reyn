"""Shim re-export — canonical module is :mod:`reyn.safe.text`.

FP-0042 backward-compat: import from reyn.api.safe.text is
forwarded to the canonical reyn.safe.text. Scheduled for removal
in the next release — please migrate to from reyn.safe import text.
"""
from reyn.safe.text import *  # noqa: F401,F403 — re-export
