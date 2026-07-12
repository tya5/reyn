"""Part-type marker: hook — a reactive hook-event registration (input role).

The live registry is ``reyn.hooks.loader.load_hooks`` (config ``hooks`` → a
``HookRegistry`` of trigger→action glue); a hook is reactive ingress, so it
plays the input role (proposal §2.1).
"""
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name="hook",
    roles=frozenset({"input"}),
    category="hook",
    registry_ref="reyn.hooks.loader:load_hooks",
    description="A reactive hook-event registration (trigger + action glue).",
)
