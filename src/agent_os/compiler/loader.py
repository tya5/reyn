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


def _stdlib_dir(kind: str) -> Path:
    """Return the installed stdlib/<kind> directory via importlib.resources."""
    import importlib.resources
    return Path(importlib.resources.files("stdlib") / kind)  # type: ignore[arg-type]


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


def load_dsl_app(
    app_md_path: str | Path,
    dsl_root: str | Path | None = None,
    _loading_stack: frozenset[str] | None = None,
) -> App:
    """
    Compile a Markdown App DSL file into a runtime App object.

    Directory resolution order:
      1. <app_dir>/artifacts/  and  <app_dir>/phases/       (app-local)
      2. <dsl_root>/shared/artifacts/  and  .../phases/     (inferred shared)
      3. <cwd>/dsl/shared/artifacts/   and  .../phases/     (project fallback)

    The project fallback lets workspace-generated apps reference shared artifacts
    (e.g. user_message) without requiring --dsl-root.

    app_md_path  : path to the app .md file  (dsl/apps/<name>/app.md)
    dsl_root     : root of the dsl/ tree. Defaults to <app_dir>.parent.parent.
    """
    app_path = Path(app_md_path)
    app_dir = app_path.parent

    if dsl_root is None:
        dsl_root = app_dir.parent.parent
    dsl_root = Path(dsl_root)

    local_artifacts_dir = app_dir / "artifacts"
    local_phases_dir    = app_dir / "phases"

    shared_artifact_dirs = _collect_shared_dirs(dsl_root, "artifacts")
    shared_phase_dirs    = _collect_shared_dirs(dsl_root, "phases")

    artifact_search_dirs = [local_artifacts_dir] + shared_artifact_dirs
    phase_search_dirs    = [local_phases_dir]    + shared_phase_dirs

    app_def = parse_app(app_path)

    # Load artifacts: shared dirs first (earlier = lower priority), then app local
    artifact_defs: dict[str, ArtifactDef] = {}
    for d in reversed(shared_artifact_dirs):   # project fallback < inferred shared < local
        _load_dir(d, parse_artifact, artifact_defs)
    _load_dir(local_artifacts_dir, parse_artifact, artifact_defs)

    _validate_references(artifact_defs)
    _check_circular(artifact_defs)

    # Load phases: shared dirs first, then app local
    phase_defs: dict[str, PhaseDef] = {}
    for d in reversed(shared_phase_dirs):
        _load_dir(d, parse_phase, phase_defs)
    _load_dir(local_phases_dir, parse_phase, phase_defs)

    # Resolve app nodes: load each sub-app for its entry schema; detect cycles
    from agent_os.models import AppNodeSpec
    loading_stack = _loading_stack or frozenset()
    abs_app_path = str(app_path.resolve())
    loading_stack = loading_stack | {abs_app_path}

    app_node_specs: dict[str, AppNodeSpec] = {}
    for node_id, node_def in app_def.app_nodes.items():
        sub_app_path = dsl_root / "apps" / node_def.app_name / "app.md"
        abs_sub = str(sub_app_path.resolve())
        if abs_sub in loading_stack:
            cycle = " → ".join(list(loading_stack) + [abs_sub])
            raise ValueError(f"Circular app dependency detected: {cycle}")
        sub_app = load_dsl_app(sub_app_path, dsl_root=dsl_root, _loading_stack=loading_stack)
        entry_phase = sub_app.phases[sub_app.entry_phase]
        app_node_specs[node_id] = AppNodeSpec(
            app_path=abs_sub,
            dsl_root=str(dsl_root),
            workspace=node_def.workspace,
            entry_input_schema=entry_phase.input_schema,
            entry_input_schema_name=entry_phase.input_schema_name,
            entry_input_description=entry_phase.input_description,
        )

    # Determine which phases the app uses (exclude app node IDs)
    used_phases: set[str] = {app_def.entry}
    for src, dst in app_def.edges:
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
            raise _not_found_error(missing[0], artifact_search_dirs, "Artifact")
        input_arts = [artifact_defs[n] for n in pd.inputs]
        phase_objects[name] = expand_phase(pd, input_arts, artifact_defs)

    if app_def.final_output and app_def.final_output not in artifact_defs:
        raise _not_found_error(app_def.final_output, artifact_search_dirs, "Artifact")

    return expand_app(app_def, phase_defs, artifact_defs, phase_objects, app_node_specs)
