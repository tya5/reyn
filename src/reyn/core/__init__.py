"""reyn.core — the OS runtime engine (P3).

Domain grouping (broad-#5 regroup, issue #1682) for the packages that
constitute the OS's execution core, as distinct from the persistence
group (``reyn.data``) and the surface group (``reyn.interfaces``):

- ``kernel``      — context build, LLM call, validation, transitions
- ``op_runtime``  — Control IR op catalog and dispatch
- ``compiler``    — skill/phase DSL compilation
- ``plan``        — phase-graph planning
- ``registry``    — skill/phase resolution
- ``events``      — append-only event log + replay (P6 audit truth)

This package node is import-only; it deliberately exports nothing so the
move is a pure path regrouping with no surface change. ``reyn.core.events``
is the PACKAGE; the ``.reyn/events`` runtime directory (the P6 audit log on
disk) is a distinct, separately-named filesystem path and is unaffected.
"""
