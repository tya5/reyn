"""Part-type marker: skill — a named SKILL.md instruction set (workflow role).

The live registry is ``reyn.data.skills.registry.build_skill_registry`` (config
``skills.entries`` → ``list[SkillEntry]``); the model reads the SKILL.md body at
L2 when the skill is relevant.
"""
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name="skill",
    roles=frozenset({"workflow"}),
    category="skill_management",
    registry_ref="reyn.data.skills.registry:build_skill_registry",
    description="A named SKILL.md instruction set, read by the model at L2.",
)
