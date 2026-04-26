from pathlib import Path
from .parser import parse_artifact, parse_phase, parse_app
from .expander import expand_phase, expand_app, _TYPE_MAP
from .ir import ArtifactDef, PhaseDef
from agent_os.models import App


def _validate_references(artifact_defs: dict[str, ArtifactDef]) -> None:
    known_primitives = set(_TYPE_MAP.keys())
    errors: list[str] = []
    for art in artifact_defs.values():
        for f in art.fields:
            if f.type_str not in known_primitives and f.type_str not in artifact_defs:
                errors.append(
                    f"  artifact '{art.name}' → field '{f.name}': unknown type '{f.type_str}'"
                )
    if errors:
        raise ValueError("Unknown artifact references:\n" + "\n".join(errors))


def _check_circular(artifact_defs: dict[str, ArtifactDef]) -> None:
    def dfs(name: str, visited: set[str], stack: list[str]) -> None:
        if name not in artifact_defs:
            return
        stack.append(name)
        for f in artifact_defs[name].fields:
            if f.type_str in artifact_defs:
                if f.type_str in stack:
                    cycle = " → ".join(stack + [f.type_str])
                    raise ValueError(f"Circular artifact reference: {cycle}")
                if f.type_str not in visited:
                    dfs(f.type_str, visited, stack)
        stack.pop()
        visited.add(name)

    visited: set[str] = set()
    for name in artifact_defs:
        if name not in visited:
            dfs(name, visited, [])


def _not_found_error(name: str, search_dirs: list[Path], kind: str) -> ValueError:
    """Produce a clear error that lists every location that was searched."""
    lines = [f"{kind} '{name}' not found.", "Searched:"]
    for d in search_dirs:
        lines.append(f"  - {d / (name + '.md')}")
    return ValueError("\n".join(lines))


def _load_dir(directory: Path, parser, registry: dict) -> None:
    """Parse every .md file in directory and add to registry (overwrites on conflict)."""
    if not directory.exists():
        return
    for md in sorted(directory.glob("*.md")):
        item = parser(md)
        registry[item.name] = item


def load_dsl_app(app_md_path: str | Path, dsl_root: str | Path | None = None) -> App:
    """
    Compile a Markdown App DSL file into a runtime App object.

    Directory resolution order (app local overrides shared):
      1. <app_dir>/artifacts/   and  <app_dir>/phases/
      2. <dsl_root>/shared/artifacts/  and  <dsl_root>/shared/phases/

    app_md_path  : path to the app .md file
                   new layout : dsl/apps/<name>/app.md
    dsl_root     : root of the dsl/ tree.
                   Defaults to <app_dir>.parent.parent  (i.e. dsl/).
    """
    app_path = Path(app_md_path)
    app_dir = app_path.parent                 # dsl/apps/writing_review_app/

    if dsl_root is None:
        dsl_root = app_dir.parent.parent      # dsl/
    dsl_root = Path(dsl_root)

    shared_artifacts_dir = dsl_root / "shared" / "artifacts"
    shared_phases_dir    = dsl_root / "shared" / "phases"
    local_artifacts_dir  = app_dir / "artifacts"
    local_phases_dir     = app_dir / "phases"

    artifact_search_dirs = [local_artifacts_dir, shared_artifacts_dir]
    phase_search_dirs    = [local_phases_dir,    shared_phases_dir]

    app_def = parse_app(app_path)

    # Load artifacts: shared first, then app local (local overwrites shared)
    artifact_defs: dict[str, ArtifactDef] = {}
    _load_dir(shared_artifacts_dir, parse_artifact, artifact_defs)
    _load_dir(local_artifacts_dir,  parse_artifact, artifact_defs)

    _validate_references(artifact_defs)
    _check_circular(artifact_defs)

    # Load phases: shared first, then app local
    phase_defs: dict[str, PhaseDef] = {}
    _load_dir(shared_phases_dir, parse_phase, phase_defs)
    _load_dir(local_phases_dir,  parse_phase, phase_defs)

    # Determine which phases the app uses
    used_phases: set[str] = {app_def.entry}
    for src, dst in app_def.edges:
        used_phases.update([src, dst])

    # Validate and expand each used phase
    phase_objects = {}
    for name in used_phases:
        if name not in phase_defs:
            raise _not_found_error(name, phase_search_dirs, "Phase")
        pd = phase_defs[name]
        missing = [n for n in pd.inputs if n not in artifact_defs]
        if missing:
            raise _not_found_error(missing[0], artifact_search_dirs, "Artifact")
        input_arts = [artifact_defs[n] for n in pd.inputs]
        phase_objects[name] = expand_phase(pd, input_arts, artifact_defs)

    if app_def.final_output and app_def.final_output not in artifact_defs:
        raise _not_found_error(app_def.final_output, artifact_search_dirs, "Artifact")

    return expand_app(app_def, phase_defs, artifact_defs, phase_objects)
