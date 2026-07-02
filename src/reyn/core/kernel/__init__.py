"""kernel — code-execution runtime submodules (codeact + python-step harness).

This ``__init__`` performs NO eager imports, so importing a *submodule* —
notably ``reyn.core.kernel._python_harness``, the python preprocessor-step child
entry point — does not pull any heavier chain in through this package root.
Submodule imports (``from reyn.core.kernel.codeact_runner import CodeActRunner``,
``from reyn.core.kernel.sub_loop_memo_key import ...``) are used directly and are
unaffected.
"""
from __future__ import annotations
