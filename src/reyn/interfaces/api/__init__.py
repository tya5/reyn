"""Reyn-provided API packages for python preprocessor steps.

- ``reyn.interfaces.api.safe.*``  — callable from ``mode: safe`` python steps
                         (= AST validator allows the import).
- ``reyn.interfaces.api.unsafe.*`` — callable from ``mode: unsafe`` python steps
                         (= AST validator rejects from ``safe`` mode).

These packages run inside the python step's subprocess. Permission
was granted at parent level when the step's mode was approved at
startup; individual calls are NOT audited per-invocation
(= step-level audit only; see FP-0015 for per-call audit).
"""
