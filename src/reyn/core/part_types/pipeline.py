"""Part-type marker: pipeline — a registered multi-step orchestration DSL
(workflow role).

The live registry is ``reyn.data.pipelines.registry.build_pipeline_registry``
(config ``pipelines.entries`` → a populated ``PipelineRegistry`` of parsed
``Pipeline`` objects).
"""
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name="pipeline",
    roles=frozenset({"workflow"}),
    category="pipeline_management",
    registry_ref="reyn.data.pipelines.registry:build_pipeline_registry",
    description="A registered multi-step orchestration DSL document.",
    doc_ref="docs/reference/runtime/pipeline-dsl.md",
)
