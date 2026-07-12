"""Part-type marker: presentation — a named, operator-facing render template
(output role).

The live registry is
``reyn.data.presentations.registry.build_presentation_registry`` (config
``presentations.entries`` → a ``PresentationRegistry`` of validated blueprint
node lists); a template renders operator-facing output at zero token cost.
"""
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name="presentation",
    roles=frozenset({"output"}),
    category="presentation",
    registry_ref="reyn.data.presentations.registry:build_presentation_registry",
    description="A named, operator-facing presentation/render template.",
    doc_ref="docs/concepts/runtime/present.md",
)
