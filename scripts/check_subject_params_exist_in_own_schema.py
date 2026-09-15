#!/usr/bin/env python3
"""#6184 段3-1 — every name a ``ToolDefinition.subject_params`` declares
must be a real property in that SAME tool's own
``parameters["properties"]``.

## The bug class this closes

``subject_params`` is a POINTER, not new content — it names which of a
tool's own already-declared JSON-schema parameter names carries the
"主役" (subject) a flowview consumer should draw a tool row's display
from (#6184 段3). A pointer can go STALE the same way any cross-reference
can: a tool's own schema renames/removes a parameter (an ordinary,
otherwise-unremarkable change), and nothing forces the ``subject_params``
declaration sitting a few lines above it to be updated in the same PR.
Left unchecked, that produces a ``ToolDefinition`` whose own declared
subject param no longer resolves against its own arguments at call
time — a defect the field's own author (or the next person who touches
that tool's schema) has no way to notice without reading both sites side
by side.

This mirrors ``gutter.py:_is_retrieval_tool``'s own discipline
(verbatim: "derived from ``ToolDefinition.purity``, never a hardcoded
name list") one level further: the CHECK itself is derived from the
tool's own schema, not a second, hand-maintained list this script would
otherwise have to keep in sync by hand.

## Why this is NOT the #6190-rejected shape

#6190 (this same arc, `_REASONING_BUNDLE_FIELDS`-adjacent precedent
aside) rejected a hand-curated field-name list living in a SEPARATE
file from the thing it describes (the "#6162/#6179 curated list drifts
when it lives in 2 places" shape, hit twice already in this codebase).
``subject_params`` differs on all 4 axes lead-coder's own dispatch named:
① it declares a POINTER (which existing param is the subject), not new
content; ② it lives ON the tool's own ``ToolDefinition``, in
``tools/types.py``, not in a separate ``descriptions/`` module; ③ a stale
declaration is directly visible in TODAY's display the moment 段3 wires
a reader (a subject pointing at a param that no longer exists renders as
nothing, not as a delayed abstract risk); ④ this gate makes the
cross-reference mechanically checkable at CI time, the same way this
script does.

## Population: derived from the real registry, no allowlist

Every ``ToolDefinition`` in ``get_default_registry()`` whose
``subject_params`` is non-empty is checked. Today that population is
EMPTY — 段3-1 (this stage) declares the field but declares no tools'
values yet (accept ④: the real declarations are 段3-4, owner-judgment-
gated); the gate's own diff is a no-op until then, and stays a no-op
for any tool that never opts in. There is no allowlist to grow: a name
either resolves against its own tool's ``parameters["properties"]`` or
it does not.

CI: gate
"""
from __future__ import annotations

# #3024: verify the in-process `reyn` this bare-python script is about
# to import (module-level or lazily, anywhere below) resolves THIS
# checkout, not whichever tree the ambient venv's editable install
# happens to point at. Exits loudly (never a silent wrong-tree run) on
# a mismatch -- see verify_env_identity.py's own guard_bare_script_or_exit
# docstring. Population + presence are enforced mechanically by
# check_scripts_import_identity_guard.py, so this call cannot be dropped
# without the gate catching it.
from verify_env_identity import guard_bare_script_or_exit

guard_bare_script_or_exit()

import sys


def find_stale_subject_params(registry=None) -> "list[tuple[str, str]]":
    """``(tool_name, offending_param_name)`` pairs for every declared
    ``subject_params`` entry that does not exist in that SAME tool's own
    ``parameters["properties"]`` — the failure set, sorted for a stable
    diagnostic order. *registry* is injectable (defaults to the real
    :func:`reyn.tools.get_default_registry`) so a test can pass a
    constructed registry and still reach this function through
    :func:`main`."""
    if registry is None:
        from reyn.tools import get_default_registry
        registry = get_default_registry()

    offenders: "list[tuple[str, str]]" = []
    for tool in registry:
        if not tool.subject_params:
            continue
        properties = (tool.parameters or {}).get("properties") or {}
        for param_name in tool.subject_params:
            if param_name not in properties:
                offenders.append((tool.name, param_name))
    return sorted(offenders)


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-registry cross-check, no target to name

    from reyn.tools import get_default_registry
    registry = get_default_registry()
    offenders = find_stale_subject_params(registry)
    declared_count = sum(1 for t in registry if t.subject_params)

    if not offenders:
        print(
            "OK: every declared subject_params name resolves against its "
            f"own tool's parameters[\"properties\"] ({declared_count} "
            f"tool(s) with a non-empty declaration, {len(list(registry))} "
            "tool(s) total)."
        )
        return 0

    print("subject-params-exist-in-own-schema gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} subject_params entry(ies) name a parameter "
        "that does not exist in that SAME tool's own "
        "parameters[\"properties\"] (#6184 段3-1):",
        file=sys.stderr,
    )
    for tool_name, param_name in offenders:
        print(f"  {tool_name}: {param_name!r}", file=sys.stderr)
    print(
        "\nA subject_params declaration is a POINTER at one of the tool's "
        "OWN already-declared parameter names, never new content — when "
        "the tool's own schema renames or removes that parameter, the "
        "pointer goes stale silently unless caught here. Fix by either:\n"
        "\n"
        "  (a) updating the subject_params declaration to name the "
        "parameter's CURRENT name, in the SAME PR that renamed it; or\n"
        "  (b) removing the stale name from subject_params if that "
        "parameter is no longer the tool's subject at all.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
