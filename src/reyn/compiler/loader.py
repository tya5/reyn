from pathlib import Path
from .parser import parse_artifact, parse_phase, parse_app
from .expander import expand_phase, expand_app
from .ir import ArtifactDef, PhaseDef
from .preprocessor_typing import infer_llm_visible_schema, PreprocessorTypeError
from reyn.models import App


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


def _find_preprocessor_app_names(phase_objects: dict) -> set[str]:
    """Collect all sub-app names referenced by any phase's preprocessor."""
    from reyn.models import RunAppStep, IterateStep
    names: set[str] = set()

    def _collect(step) -> None:
        if isinstance(step, RunAppStep):
            names.add(step.app)
        elif isinstance(step, IterateStep):
            _collect(step.apply)

    for phase in phase_objects.values():
        for step in phase.preprocessor:
            _collect(step)
    return names


def _resolve_preprocessor_sub_apps(
    phase_objects: dict,
    dsl_root: Path,
    loading_stack: frozenset[str],
) -> dict[str, App]:
    """Load every sub-app referenced in preprocessors. Returns name → App."""
    app_names = _find_preprocessor_app_names(phase_objects)
    sub_apps: dict[str, App] = {}
    for name in app_names:
        # Search order: dsl_root/apps/<name>/app.md then stdlib/apps/<name>/app.md
        candidates = [
            dsl_root / "apps" / name / "app.md",
            Path(_stdlib_dir("apps")) / name / "app.md",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            searched = [str(p) for p in candidates]
            raise ValueError(
                f"Preprocessor sub-app '{name}' not found.\nSearched:\n"
                + "\n".join(f"  - {p}" for p in searched)
            )
        abs_path = str(path.resolve())
        if abs_path in loading_stack:
            cycle = " → ".join(list(loading_stack) + [abs_path])
            raise ValueError(f"Circular preprocessor dependency detected: {cycle}")
        sub_apps[name] = load_dsl_app(path, dsl_root=dsl_root, _loading_stack=loading_stack)
    return sub_apps


def _infer_preprocessor_schemas(
    phase_objects: dict,
    preprocessor_sub_apps: dict[str, App],
) -> dict:
    """Validate preprocessor chains at compile time; return phase_objects unchanged."""
    for name, phase in phase_objects.items():
        if not phase.preprocessor:
            continue
        try:
            infer_llm_visible_schema(
                phase.input_schema, phase.preprocessor, preprocessor_sub_apps
            )
        except PreprocessorTypeError as exc:
            raise ValueError(f"Phase '{name}': {exc}") from exc
    return phase_objects


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
        _load_dir(d, parse_artifact, artifact_defs, glob="*.yaml")
    _load_dir(local_artifacts_dir, parse_artifact, artifact_defs, glob="*.yaml")

    # Load phases: shared dirs first, then app local
    phase_defs: dict[str, PhaseDef] = {}
    for d in reversed(shared_phase_dirs):
        _load_dir(d, parse_phase, phase_defs)
    _load_dir(local_phases_dir, parse_phase, phase_defs)

    # Resolve app nodes: load each sub-app for its entry schema; detect cycles
    from reyn.models import AppNodeSpec
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
            raise _not_found_error(missing[0], artifact_search_dirs, "Artifact", ext=".yaml")
        input_arts = [artifact_defs[n] for n in pd.inputs]
        phase_objects[name] = expand_phase(pd, input_arts)

    if app_def.final_output and app_def.final_output not in artifact_defs:
        raise _not_found_error(app_def.final_output, artifact_search_dirs, "Artifact", ext=".yaml")

    # Resolve preprocessor sub-apps and run schema inference for each phase
    preprocessor_sub_apps = _resolve_preprocessor_sub_apps(
        phase_objects, dsl_root, loading_stack
    )
    phase_objects = _infer_preprocessor_schemas(phase_objects, preprocessor_sub_apps)

    return expand_app(
        app_def, phase_defs, artifact_defs, phase_objects,
        app_node_specs, preprocessor_sub_apps,
    )
