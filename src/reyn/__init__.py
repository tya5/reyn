"""reyn — Agent OS package root.

This ``__init__`` performs NO eager imports, so importing a *submodule* —
notably ``reyn.core.kernel._codeact_harness``, the CodeAct sandbox child-process
entry point — does not pull the agent / llm / httpx chain in through the package
root. (FP-0008 C4: keeping the harness cold-import path well under the
python-step timeout.)

It DOES set two ``os.environ`` defaults (no import cost) before anything else
runs, because they must exist before litellm's own package init executes:
litellm's ``__init__.py`` calls ``get_model_cost_map(...)`` and (lazily,
on first Anthropic call) the beta-headers manager at *module import time*, each
doing a network fetch of a remote config unless the corresponding
``LITELLM_LOCAL_*`` env var is already set. Every ``import litellm`` in this
codebase is lazy (inside functions, in ``reyn.llm.*`` and friends) and none of
them precede a `reyn` submodule import, so this package root — which Python
guarantees runs before any ``reyn.*`` submodule is importable — is the
earliest point that is still guaranteed to run before the first
``import litellm`` on every startup path (CLI, tests, scripts).
"""
from __future__ import annotations

import os

# Silence litellm's startup network fetches (remote cost-table / remote beta-
# headers config) by defaulting to its bundled local snapshots. ``setdefault``
# (not a forced assignment) so an operator who explicitly wants the remote
# fetch (e.g. sets the var to a falsey value themselves) is respected — see
# reyn's no-uncustomizable-hardcodes rule. Must run before litellm is imported
# anywhere; see the module docstring above for why this is the right place.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS", "True")
