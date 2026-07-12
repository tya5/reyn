"""reyn.builtin — the code-shipped builtin tier (proposal 0060 Phase 1 F3a,
Addendum A1/A2/A3).

Skills / pipelines / presentations have historically been registered PURELY
via operator config (``skills.entries`` / ``pipelines.entries`` /
``presentations.entries``) — no package-shipped tier existed below
``reyn.yaml`` (unlike hooks, whose builtin points ship code-side via
``reyn.hooks.schema_registry.BUILTIN_HOOK_SCHEMAS``). This package is that
missing tier for the other three part-types: :mod:`reyn.builtin.registry`
holds the code-shipped ``BUILTIN_SKILLS`` / ``BUILTIN_PIPELINES`` /
``BUILTIN_PRESENTATIONS`` maps and :func:`~reyn.builtin.registry.build_builtin_config`,
merged as the LOWEST config tier in ``reyn.config.loader.load_config`` — below
every operator config file, so any operator declaration overrides a
same-name builtin.

This package physically repurposes the dead ``stdlib/**/*`` package-data glob
(``pyproject.toml`` — abolished, Addendum A2) to ``builtin/**/*``, so builtin
content shipped here reaches installed wheels.

F3a (this phase) ships the MECHANISM only — ``BUILTIN_SKILLS`` /
``BUILTIN_PIPELINES`` / ``BUILTIN_PRESENTATIONS`` are empty dicts. F3b (a
later phase) populates them with the actual exemplar content (proposal §3
F3). An empty builtin tier is a valid, inert, fully-tested state.
"""
from __future__ import annotations
