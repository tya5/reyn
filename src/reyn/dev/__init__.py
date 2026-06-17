"""reyn.dev — development & measurement tooling.

Domain grouping (broad-#5 regroup, issue #1682) for the packages used to
develop, evaluate, and stress-test the OS — as distinct from the runtime
core (``reyn.core``), persistence (``reyn.data``), and user surfaces
(``reyn.interfaces``):

- ``eval``     — evaluation harness / scoring
- ``dogfood``  — dogfood-run instrumentation and helpers
- ``testing``  — shared test-support utilities

This is the ``src/reyn/dev`` PACKAGE. The repo-root ``dogfood/`` directory
(driver scripts + fixtures, listed in pyproject ``norecursedirs``) is a
separate, unmoved path.

Import-only node; exports nothing (pure path regroup, no surface change).
"""
