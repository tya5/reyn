from pathlib import Path
from .parser import parse_artifact, parse_phase, parse_skill
from .expander import expand_phase, expand_skill
from .ir import ArtifactDef, PhaseDef
from .preprocessor_typing import infer_llm_visible_schema, PreprocessorTypeError
from reyn.schemas.models import Skill


def _not_found_error(name: str, search_dirs: list[Path], kind: str, ext: str = ".md") -> ValueError:
    """Produce a clear error that lists every location that was searched."""
    lines = [f"{kind} '{name}' not found.", "Searched:"]
    for d in search_dirs:
        lines.append(f"  - {d / (name + ext)}")
    return ValueError("\n".join(lines))


def _load_dir(directory: Path, parser, registry: dict, glob: str = "*.md") -> None:
    """Parse every matching file in directory and add to registry (overwrites on conflict)."""
    if not directory.exists():
        return
    for f in sorted(directory.glob(glob)):
        item = parser(f)
        registry[item.name] = item


def _stdlib_dir(kind: str) -> Path:
    """Return the installed stdlib/<kind> directory via importlib.resources."""
    import importlib.resources
    return Path(importlib.resources.files("reyn") / "stdlib" / kind)  # type: ignore[arg-type]


def _collect_shared_dirs(dsl_root: Path, kind: str) -> list[Path]:
    """
    Return shared/<kind> directories to search in priority order (lowest first):
      1. installed stdlib/<kind>
      2. dsl_root/shared/<kind>   (user-provided dsl root)
    """
    stdlib = _stdlib_dir(kind)
    dsl_shared = dsl_root / "shared" / kind
    dirs: list[Path] = []
    if stdlib.exists():
        dirs.append(stdlib)
    if dsl_shared.resolve() != stdlib.resolve():
        dirs.append(dsl_shared)
    return dirs


def _find_preprocessor_skill_names(phase_objects: dict) -> set[str]:
    """Collect all sub-skill names referenced by any phase's preprocessor.

    Detects both:
      - `run_op` steps wrapping a `run_skill` ControlIROp (the canonical form)
      - `iterate.apply` containing the same
    """
    from reyn.schemas.models import IterateStep, RunOpStep
    names: set[str] = set()

    def _collect(step) -> None:
        if isinstance(step, RunOpStep) and getattr(step.op, "kind", None) == "run_skill":
            names.add(step.op.skill)
        elif isinstance(step, IterateStep):
            _collect(step.apply)

    for phase in phase_objects.values():
        for step in phase.preprocessor:
            _collect(step)
    return names


def _resolve_preprocessor_sub_skills(
    phase_objects: dict,
    dsl_root: Path,
    loading_stack: frozenset[str],
) -> dict[str, Skill]:
    """Load every sub-skill referenced in preprocessors. Returns name → Skill."""
    skill_names = _find_preprocessor_skill_names(phase_objects)
    sub_skills: dict[str, Skill] = {}
    for name in skill_names:
        # Search order: dsl_root/skills/<name>/skill.md then stdlib/skills/<name>/skill.md
        candidates = [
            dsl_root / "skills" / name / "skill.md",
            Path(_stdlib_dir("skills")) / name / "skill.md",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            searched = [str(p) for p in candidates]
            raise ValueError(
                f"Preprocessor sub-skill '{name}' not found.\nSearched:\n"
                + "\n".join(f"  - {p}" for p in searched)
            )
        abs_path = str(path.resolve())
        if abs_path in loading_stack:
            cycle = " → ".join(list(loading_stack) + [abs_path])
            raise ValueError(f"Circular preprocessor dependency detected: {cycle}")
        sub_skills[name] = load_dsl_skill(path, dsl_root=dsl_root, _loading_stack=loading_stack)
    return sub_skills


def _infer_preprocessor_schemas(
    phase_objects: dict,
    preprocessor_sub_skills: dict[str, Skill],
) -> dict:
    """Validate preprocessor chains at compile time; return phase_objects unchanged."""
    for name, phase in phase_objects.items():
        if not phase.preprocessor:
            continue
        try:
            infer_llm_visible_schema(
                phase.input_schema, phase.preprocessor, preprocessor_sub_skills
            )
        except PreprocessorTypeError as exc:
            raise ValueError(f"Phase '{name}': {exc}") from exc
    return phase_objects


def load_dsl_skill(
    skill_md_path: str | Path,
    dsl_root: str | Path | None = None,
    _loading_stack: frozenset[str] | None = None,
) -> Skill:
    """
    Compile a Markdown Skill DSL file into a runtime Skill object.

    Directory resolution order:
      1. <skill_dir>/artifacts/  and  <skill_dir>/phases/       (skill-local)
      2. <dsl_root>/shared/artifacts/  and  .../phases/         (inferred shared)
      3. <cwd>/dsl/shared/artifacts/   and  .../phases/         (project fallback)

    The project fallback lets workspace-generated skills reference shared artifacts
    (e.g. user_message) without requiring --dsl-root.

    skill_md_path : path to the skill .md file  (dsl/skills/<name>/skill.md)
    dsl_root      : root of the dsl/ tree. Defaults to <skill_dir>.parent.parent.
    """
    skill_path = Path(skill_md_path)
    skill_dir = skill_path.parent

    if dsl_root is None:
        dsl_root = skill_dir.parent.parent
    dsl_root = Path(dsl_root)

    local_artifacts_dir = skill_dir / "artifacts"
    local_phases_dir    = skill_dir / "phases"

    shared_artifact_dirs = _collect_shared_dirs(dsl_root, "artifacts")
    shared_phase_dirs    = _collect_shared_dirs(dsl_root, "phases")

    artifact_search_dirs = [local_artifacts_dir] + shared_artifact_dirs
    phase_search_dirs    = [local_phases_dir]    + shared_phase_dirs

    skill_def = parse_skill(skill_path)

    # Load artifacts: shared dirs first (earlier = lower priority), then skill local
    artifact_defs: dict[str, ArtifactDef] = {}
    for d in reversed(shared_artifact_dirs):   # project fallback < inferred shared < local
        _load_dir(d, parse_artifact, artifact_defs, glob="*.yaml")
    _load_dir(local_artifacts_dir, parse_artifact, artifact_defs, glob="*.yaml")

    # Load phases: shared dirs first, then skill local
    phase_defs: dict[str, PhaseDef] = {}
    for d in reversed(shared_phase_dirs):
        _load_dir(d, parse_phase, phase_defs)
    _load_dir(local_phases_dir, parse_phase, phase_defs)

    # Resolve skill nodes: load each sub-skill for its entry schema; detect cycles
    from reyn.schemas.models import SkillNodeSpec
    loading_stack = _loading_stack or frozenset()
    abs_skill_path = str(skill_path.resolve())
    loading_stack = loading_stack | {abs_skill_path}

    skill_node_specs: dict[str, SkillNodeSpec] = {}
    for node_id, node_def in skill_def.skill_nodes.items():
        sub_skill_path = dsl_root / "skills" / node_def.skill_name / "skill.md"
        abs_sub = str(sub_skill_path.resolve())
        if abs_sub in loading_stack:
            cycle = " → ".join(list(loading_stack) + [abs_sub])
            raise ValueError(f"Circular skill dependency detected: {cycle}")
        sub_skill = load_dsl_skill(sub_skill_path, dsl_root=dsl_root, _loading_stack=loading_stack)
        entry_phase = sub_skill.phases[sub_skill.entry_phase]
        skill_node_specs[node_id] = SkillNodeSpec(
            skill_path=abs_sub,
            dsl_root=str(dsl_root),
            workspace=node_def.workspace,
            entry_input_schema=entry_phase.input_schema,
            entry_input_schema_name=entry_phase.input_schema_name,
            entry_input_description=entry_phase.input_description,
        )

    # Determine which phases the skill uses (exclude skill node IDs)
    used_phases: set[str] = {skill_def.entry}
    for src, dst in skill_def.edges:
        for node in (src, dst):
            if not node.startswith("@"):
                used_phases.add(node)

    # Validate and expand each used phase
    phase_objects = {}
    for name in used_phases:
        if name not in phase_defs:
            raise _not_found_error(name, phase_search_dirs, "Phase")
        pd = phase_defs[name]
        missing = [n for n in pd.inputs if n not in artifact_defs]
        if missing:
            raise _not_found_error(missing[0], artifact_search_dirs, "Artifact", ext=".yaml")
        input_arts = [artifact_defs[n] for n in pd.inputs]
        phase_objects[name] = expand_phase(pd, input_arts)

    if skill_def.final_output and skill_def.final_output not in artifact_defs:
        raise _not_found_error(skill_def.final_output, artifact_search_dirs, "Artifact", ext=".yaml")

    # Resolve preprocessor sub-skills and run schema inference for each phase
    preprocessor_sub_skills = _resolve_preprocessor_sub_skills(
        phase_objects, dsl_root, loading_stack
    )
    phase_objects = _infer_preprocessor_schemas(phase_objects, preprocessor_sub_skills)

    skill = expand_skill(
        skill_def, phase_defs, artifact_defs, phase_objects,
        skill_node_specs, preprocessor_sub_skills,
    )
    # Record where on disk this skill lives so runtime components (e.g. python
    # preprocessor steps) can resolve relative module paths against it.
    skill.skill_dir = str(skill_dir.resolve())
    return skill
