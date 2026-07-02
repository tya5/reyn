"""reyn — Agent OS package root.

This ``__init__`` performs NO eager imports, so importing a *submodule* —
notably ``reyn.core.kernel._python_harness``, the python preprocessor-step child
entry point — does not pull the agent / llm / httpx chain in through the package
root. (FP-0008 C4: keeping the harness cold-import path well under the
python-step timeout.)
"""
from __future__ import annotations
