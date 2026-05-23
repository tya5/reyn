"""Shim re-export — canonical module is :mod:`reyn.safe.schema`.

FP-0042 backward-compat: import from reyn.api.safe.schema is
forwarded to the canonical reyn.safe.schema. Scheduled for removal
in the next release — please migrate to from reyn.safe import schema.
"""
from reyn.safe.schema import *  # noqa: F401,F403 — re-export
