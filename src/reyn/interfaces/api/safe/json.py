"""Shim re-export — canonical module is :mod:`reyn.safe.json`.

FP-0042 backward-compat: import from reyn.interfaces.api.safe.json is
forwarded to the canonical reyn.safe.json. Scheduled for removal
in the next release — please migrate to from reyn.safe import json.
"""
from reyn.safe.json import *  # noqa: F401,F403 — re-export
