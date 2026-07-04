"""Pipeline control-plane subsystem (reyn-pipeline-v0.9).

Home for the Pipeline DSL's pure, IO-free building blocks: the expression
evaluator (`expr.py`) and the schema/type registry + validator + static
path resolver (`schema.py`, R2 of
`docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`). Both modules are
standalone — no executor, no WAL, no agent runtime wiring lives here yet;
this package is the shared foundation the thin executor vertical-slice
(R3/R4) will build on.
"""
