"""
DSL Linter

Checks DSL files for consistency issues that would make Meta-App generation unreliable.
Does not compile; reports issues without crashing.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from reyn.op_runtime.registry import ALL_OP_KINDS as _KNOWN_OP_KINDS

from .parser import _split_frontmatter, parse_artifact

PHASE_FRONTMATTER_ORDER = ["type", "name", "input", "role", "can_finish", "allowed_ops"]
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class LintIssue:
    severity: str  # "error" | "warning"
    path: Path
    message: str

    def __str__(self) -> str:
        rel = self.path
        return f"[{self.severity.upper():7}] {rel}  →  {self.message}"


def _is_snake(name: str) -> bool:
    return bool(_SNAKE_CASE.match(name))


# ── Artifact ──────────────────────────────────────────────────────────────────

def lint_artifact(path: Path, known_artifact_names: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        issues.append(LintIssue("error", path, f"Not valid YAML: {exc}"))
        return issues

    if not isinstance(data, dict):
        issues.append(LintIssue("error", path, "Artifact file must be a YAML object"))
        return issues

    name = data.get("name", "")
    if not name:
        issues.append(LintIssue("error", path, "Missing 'name' key"))
    elif not _is_snake(name):
        issues.append(LintIssue("warning", path, f"Name '{name}' should be snake_case"))

    schema = data.get("schema")
    if not schema:
        issues.append(LintIssue("error", path, "Missing 'schema' key — must be a JSON Schema object"))
        return issues

    if not isinstance(schema, dict):
        issues.append(LintIssue("error", path, "'schema' must be a YAML/JSON object"))
        return issues

    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        issues.append(LintIssue("error", path, f"Invalid JSON Schema: {exc.message}"))
        return issues

    if schema.get("type") != "object":
        issues.append(LintIssue("warning", path, "Artifact schema should have 'type: object' at top level"))

    return issues


# ── Phase ─────────────────────────────────────────────────────────────────────

def lint_phase(path: Path, known_artifacts: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)

    for key in ("type", "name"):
        if key not in fm:
            issues.append(LintIssue("error", path, f"Missing required frontmatter key '{key}'"))

    name = fm.get("name", "")
    if name and not _is_snake(name):
        issues.append(LintIssue("warning", path, f"Name '{name}' should be snake_case"))

    # Frontmatter key ordering
    actual = [k for k in fm if k in PHASE_FRONTMATTER_ORDER]
    expected = [k for k in PHASE_FRONTMATTER_ORDER if k in fm]
    if actual != expected:
        issues.append(LintIssue(
            "warning", path,
            f"Frontmatter keys not in canonical order. "
            f"Expected {expected}, got {actual}",
        ))

    if "output" in fm:
        issues.append(LintIssue(
            "error", path,
            f"Phase '{fm.get('name', path.stem)}' must not define output. "
            "Output schema is provided at runtime from candidate next phase input schemas "
            "or app final output schema.",
        ))

    # Input artifact resolution
    inputs_raw = str(fm.get("input") or "")
    inputs = [i.strip() for i in inputs_raw.split("|") if i.strip()]
    for inp in inputs:
        if inp not in known_artifacts:
            issues.append(LintIssue("error", path, f"Input artifact '{inp}' not found"))

    if not body.strip():
        issues.append(LintIssue("warning", path, "Phase has no instructions"))

    issues.extend(_lint_python_preprocessor(path, fm))
    issues.extend(_lint_allowed_ops(path, fm))

    return issues


# ── allowed_ops check ────────────────────────────────────────────────────────


def _lint_allowed_ops(path: Path, fm: dict) -> list[LintIssue]:
    """Validate the optional `allowed_ops` frontmatter list.

    Catches misspelled or invented op kinds early — the runtime would
    silently drop them with `not_allowed_in_phase`, but at lint time we
    can flag them. Empty list (`[]`) is valid: it explicitly allows no ops.
    """
    if "allowed_ops" not in fm:
        return []
    issues: list[LintIssue] = []
    raw = fm["allowed_ops"]
    if not isinstance(raw, list):
        issues.append(LintIssue(
            "error", path,
            f"allowed_ops must be a YAML list (e.g. [file, ask_user]), "
            f"got {type(raw).__name__}",
        ))
        return issues
    seen: set[str] = set()
    for i, val in enumerate(raw):
        if not isinstance(val, str):
            issues.append(LintIssue(
                "error", path,
                f"allowed_ops[{i}] must be a string op kind, got {type(val).__name__}",
            ))
            continue
        if val not in _KNOWN_OP_KINDS:
            issues.append(LintIssue(
                "warning", path,
                f"allowed_ops[{i}]='{val}' is not a known Control IR op kind. "
                f"Known kinds: {sorted(_KNOWN_OP_KINDS)}. "
                f"Unknown kinds will be silently filtered out at runtime.",
            ))
        if val in seen:
            issues.append(LintIssue(
                "warning", path,
                f"allowed_ops[{i}]='{val}' duplicates an earlier entry",
            ))
        seen.add(val)
    return issues


# ── Python preprocessor checks ────────────────────────────────────────────────


def _resolve_python_module(skill_dir: Path, module: str) -> Path | None:
    """Mirror PythonRunner._resolve_module_path's safety rules.

    Returns the resolved Path on success, None on any rejection
    (absolute, escape, missing). Caller handles "what kind of error".
    """
    if not module:
        return None
    p = Path(module)
    if p.is_absolute():
        return None
    candidate = (skill_dir / p).resolve()
    skill_resolved = skill_dir.resolve()
    try:
        candidate.relative_to(skill_resolved)
    except ValueError:
        return None
    return candidate


def _toplevel_function_names(source: str) -> set[str]:
    """Names of top-level `def` / `async def` in `source`. Empty on parse error."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _lint_python_preprocessor(phase_path: Path, fm: dict) -> list[LintIssue]:
    """Validate any `type: python` preprocessor steps in a phase frontmatter.

    Checks:
      1. Each python step has a matching permissions.python entry
         (same module + function).
      2. The declared `module` resolves to an existing file inside the
         skill directory (no absolute paths, no .. escape).
      3. The declared `function` is a top-level def in that file.
      4. (warning only) For safe-mode steps, run the harness's AST
         validator against the file and report violations.
      5. (FP-0014 hard error) Reject legacy mode keywords `pure` / `trusted`.
    """
    issues: list[LintIssue] = []
    skill_dir = phase_path.parent.parent  # phases/<x>.md → ../

    preprocessor = fm.get("preprocessor") or []
    permissions = fm.get("permissions") or {}
    perm_python = permissions.get("python") or [] if isinstance(permissions, dict) else []
    if not isinstance(preprocessor, list):
        return issues
    if not isinstance(perm_python, list):
        perm_python = []

    perm_index = {
        (str(p.get("module", "")), str(p.get("function", ""))): p
        for p in perm_python if isinstance(p, dict)
    }

    for i, step in enumerate(preprocessor):
        if not isinstance(step, dict) or step.get("type") != "python":
            continue
        module = str(step.get("module", "") or "")
        function = str(step.get("function", "") or "")
        label = f"preprocessor[{i}] python {module}:{function}"

        # Check 1 — permissions entry
        if (module, function) not in perm_index:
            issues.append(LintIssue(
                "error", phase_path,
                f"{label} is not declared in permissions.python — runtime will reject it. "
                f"Add a matching entry under `permissions.python:` with the same module and function.",
            ))
            mode = "safe"  # assume safe for the AST check below
        else:
            mode = str(perm_index[(module, function)].get("mode", "safe"))

        # Check 2 — module file path
        resolved = _resolve_python_module(skill_dir, module)
        if resolved is None:
            issues.append(LintIssue(
                "error", phase_path,
                f"{label}: module path {module!r} is not valid "
                f"(must be a relative path inside the skill directory)",
            ))
            continue
        if not resolved.exists() or not resolved.is_file():
            issues.append(LintIssue(
                "error", phase_path,
                f"{label}: module file {module!r} does not exist at {resolved}",
            ))
            continue

        try:
            source = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(LintIssue(
                "error", phase_path,
                f"{label}: cannot read module file {resolved}: {exc}",
            ))
            continue

        # Catch outright syntax errors so later checks don't silently no-op.
        try:
            ast.parse(source, filename=str(resolved))
        except SyntaxError as exc:
            issues.append(LintIssue(
                "error", phase_path,
                f"{label}: module {module!r} has a syntax error at line {exc.lineno}: {exc.msg}",
            ))
            continue

        # Check 3 — function defined at top level
        if function and function not in _toplevel_function_names(source):
            issues.append(LintIssue(
                "error", phase_path,
                f"{label}: function {function!r} is not defined as a top-level "
                f"`def` in {module!r}",
            ))

        # Check 4 — safe-mode AST validation (warning, since reyn.yaml's
        # python.allowed_modules can legitimately whitelist additional imports)
        if mode == "safe":
            try:
                from reyn.kernel._python_harness import _validate_safe_ast
            except Exception:
                # harness isn't importable in this lint context — skip silently
                continue
            try:
                _validate_safe_ast(ast.parse(source), frozenset())
            except Exception as exc:
                issues.append(LintIssue(
                    "warning", phase_path,
                    f"{label}: safe-mode check flagged module {module!r}: {exc}. "
                    f"If the module legitimately needs the flagged import, add it to "
                    f"`python.allowed_modules` in reyn.yaml.",
                ))

    return issues


# ── unsafe-without-justification (FP-0014 Component F warn rule) ──────────────

# Canonical stdlib prefix.  Skills under this path are skipped because stdlib
# intentionally uses unsafe mode and will be covered by the future hard-error
# rule ``unsafe-in-stdlib`` once the stdlib refactor is complete.
_STDLIB_SKILLS_PREFIX = "src/reyn/stdlib/"


def _is_stdlib_skill(path: Path) -> bool:
    """Return True if *path* is inside the stdlib skills tree.

    Comparison is done on the resolved POSIX path as a simple string-contains
    check so it works regardless of where the project is checked out.
    """
    posix = path.resolve().as_posix()
    # Accept either the normalised prefix or an absolute path that contains it.
    return "/src/reyn/stdlib/" in posix


def _lint_unsafe_without_justification(skill_path: Path, permissions: dict) -> list[LintIssue]:
    """Warn when a user skill's python permission entry uses mode: unsafe
    without an ``unsafe_reason`` annotation.

    Rule: ``unsafe-without-justification``
    Severity: warning (never hard-error — stdlib refactor not yet complete)

    Annotation form: add ``unsafe_reason: "<reason>"`` to the same
    permissions.python entry that declares ``mode: unsafe``.  Example::

        permissions:
          python:
            - module: ./my_helper.py
              function: run
              mode: unsafe
              unsafe_reason: "Needs network access to call external API"

    The field is a YAML scalar so it is grep-able (``grep unsafe_reason``)
    and schema-checkable, unlike a free-form comment.

    Stdlib skills (``src/reyn/stdlib/``) are excluded — they use unsafe mode
    legitimately and will be covered by the separate ``unsafe-in-stdlib`` hard
    error rule once the stdlib refactor lands.
    """
    if _is_stdlib_skill(skill_path):
        return []

    issues: list[LintIssue] = []
    perm_python = permissions.get("python") or []
    if not isinstance(perm_python, list):
        return issues

    for i, entry in enumerate(perm_python):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("mode", "safe")) != "unsafe":
            continue
        reason = entry.get("unsafe_reason")
        if not reason or not str(reason).strip():
            module = entry.get("module", f"<entry {i}>")
            function = entry.get("function", "")
            label = f"{module}:{function}" if function else module
            issues.append(LintIssue(
                "warning", skill_path,
                f"permissions.python[{i}] ({label}) uses mode: unsafe without an "
                f"unsafe_reason annotation. Add `unsafe_reason: \"<reason>\"` to "
                f"document why unsafe is required. "
                f"(rule: unsafe-without-justification, FP-0014 Component F)",
            ))
    return issues


# ── App ───────────────────────────────────────────────────────────────────────

def _find_cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    """Return the cycle path as a list of node names, or None if the graph is acyclic."""
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, [])  # ensure every node appears

    visited: set[str] = set()
    path: list[str] = []
    on_path: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        visited.add(node)
        path.append(node)
        on_path.add(node)
        for neighbour in adjacency.get(node, []):
            if neighbour not in visited:
                result = dfs(neighbour)
                if result is not None:
                    return result
            elif neighbour in on_path:
                # Found cycle — extract the cycle portion from path
                cycle_start = path.index(neighbour)
                return path[cycle_start:] + [neighbour]
        path.pop()
        on_path.discard(node)
        return None

    for node in list(adjacency):
        if node not in visited:
            result = dfs(node)
            if result is not None:
                return result
    return None


def lint_skill(path: Path, known_artifacts: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    from .parser import parse_skill
    try:
        app_def = parse_skill(path)
    except KeyError as exc:
        return [LintIssue("error", path, f"Missing required field {exc}")]
    except Exception as exc:
        return [LintIssue("error", path, f"Parse error: {exc}")]

    app_dir = path.parent
    phase_files = {p.stem for p in (app_dir / "phases").glob("*.md")} if (app_dir / "phases").exists() else set()

    # name
    if not app_def.name:
        issues.append(LintIssue("error", path, "Missing required field 'name'"))
    elif not _is_snake(app_def.name):
        issues.append(LintIssue("warning", path, f"Name '{app_def.name}' should be snake_case"))

    # entry
    if not app_def.entry:
        issues.append(LintIssue("error", path, "Missing required field 'entry'"))
    elif app_def.entry not in phase_files:
        issues.append(LintIssue("error", path, f"entry '{app_def.entry}' not found in phases/"))

    # final_output
    if not app_def.final_output:
        issues.append(LintIssue("error", path, "Missing required field 'final_output'"))
    elif app_def.final_output not in known_artifacts:
        issues.append(LintIssue(
            "error", path,
            f"final_output '{app_def.final_output}' not found in known artifacts.",
        ))

    # graph: all referenced phases must exist
    for src, dst in app_def.edges:
        for node in (src, dst):
            if node.startswith("@"):
                continue
            if node not in phase_files:
                issues.append(LintIssue(
                    "error", path,
                    f"Graph references phase '{node}' but no phases/{node}.md found.",
                ))

    # graph: entry must appear in the graph (skip for single-phase apps with no edges)
    if app_def.edges:
        all_graph_nodes = {n for edge in app_def.edges for n in edge if not n.startswith("@")}
        if app_def.entry and app_def.entry not in all_graph_nodes:
            issues.append(LintIssue(
                "warning", path,
                f"entry '{app_def.entry}' does not appear in graph — is it connected?",
            ))

    # graph: DAG check
    cycle = _find_cycle(app_def.edges)
    if cycle is not None:
        cycle_str = " → ".join(cycle)
        issues.append(LintIssue(
            "error", path,
            f"Graph contains a cycle: {cycle_str}. "
            "Use control.type='rollback' for revision loops instead of back-edges.",
        ))

    # unsafe-without-justification (FP-0014 Component F)
    issues.extend(_lint_unsafe_without_justification(path, app_def.permissions))

    return issues


# ── Plan-level lint (operates on skill_structure / skill_plan dicts) ─────────

def lint_plan(plan: dict) -> list[str]:
    """
    Run deterministic structural checks on a skill_structure / skill_plan dict.
    Returns a list of human-readable issue strings (empty if clean).

    Checks:
      - graph cycles (transitions form a DAG)
      - input_artifact coverage (every phase's input_artifact must be declared
        in `artifacts`, or be the stdlib `user_message`, or the final_output)
      - entry_phase exists in phases
      - transition endpoints reference declared phases
    """
    issues: list[str] = []

    transitions = plan.get("transitions") or []
    edges: list[tuple[str, str]] = []
    for t in transitions:
        if not isinstance(t, dict):
            continue
        src = t.get("from")
        for dst in t.get("to") or []:
            if src and dst:
                edges.append((src, dst))

    cycle = _find_cycle(edges)
    if cycle is not None:
        cycle_str = " → ".join(cycle)
        issues.append(
            f"Graph contains a cycle: {cycle_str}. "
            "Use control.type='rollback' for revision loops instead of back-edges."
        )

    phases = plan.get("phases") or []
    phase_names = {p.get("name") for p in phases if isinstance(p, dict) and p.get("name")}

    artifact_names: set[str] = set()
    for a in plan.get("artifacts") or []:
        if isinstance(a, dict) and a.get("name"):
            artifact_names.add(a["name"])
    final_output = plan.get("final_output") or {}
    if isinstance(final_output, dict) and final_output.get("name"):
        artifact_names.add(final_output["name"])
    artifact_names.add("user_message")  # stdlib

    for p in phases:
        if not isinstance(p, dict):
            continue
        input_artifact = p.get("input_artifact")
        if input_artifact and input_artifact not in artifact_names:
            issues.append(
                f"Phase '{p.get('name')}' references input_artifact "
                f"'{input_artifact}' but it is not declared in `artifacts` "
                f"(and is not the stdlib `user_message` or `final_output`)."
            )

    entry_phase = plan.get("entry_phase")
    if entry_phase and entry_phase not in phase_names:
        issues.append(
            f"entry_phase '{entry_phase}' is not declared in `phases`."
        )

    for t in transitions:
        if not isinstance(t, dict):
            continue
        src = t.get("from")
        if src and src not in phase_names:
            issues.append(f"Transition source '{src}' is not declared in `phases`.")
        for dst in t.get("to") or []:
            if dst and dst not in phase_names:
                issues.append(f"Transition target '{dst}' is not declared in `phases`.")

    return issues


# ── DSL root ──────────────────────────────────────────────────────────────────

def lint_skill_dir(skill_dir: Path) -> list[LintIssue]:
    """Lint the skill at skill_dir (must contain skill.md).

    Stdlib artifacts/phases are loaded as known names for reference resolution
    but are NOT themselves linted — only the target skill's own files are checked.
    """
    app_dir = skill_dir  # internal alias
    issues: list[LintIssue] = []

    from .loader import _stdlib_dir

    # ── known names (for reference resolution only, not linted) ──────────────
    reference_dirs: list[Path] = [
        _stdlib_dir("artifacts"),
        app_dir.parent.parent / "shared" / "artifacts",  # reyn/shared/artifacts if exists
    ]
    artifact_names: set[str] = set()
    for d in reference_dirs:
        if d.exists():
            for p in sorted(d.glob("*.yaml")):
                try:
                    art = parse_artifact(p)
                    artifact_names.add(art.name)
                except Exception:
                    pass  # broken stdlib files are not this app's problem

    # ── app-owned artifacts (collected first so names are known before lint) ──
    app_artifact_dir = app_dir / "artifacts"
    app_artifact_paths: list[Path] = []
    if app_artifact_dir.exists():
        for p in sorted(app_artifact_dir.glob("*.yaml")):
            try:
                art = parse_artifact(p)
                artifact_names.add(art.name)
                app_artifact_paths.append(p)
            except Exception as exc:
                issues.append(LintIssue("error", p, f"Parse error: {exc}"))

    # ── lint app-owned artifacts ──────────────────────────────────────────────
    for p in app_artifact_paths:
        try:
            issues.extend(lint_artifact(p, artifact_names))
        except Exception as exc:
            issues.append(LintIssue("error", p, f"Lint error: {exc}"))

    # ── lint app-owned phases ─────────────────────────────────────────────────
    app_phase_dir = app_dir / "phases"
    if app_phase_dir.exists():
        for p in sorted(app_phase_dir.glob("*.md")):
            try:
                issues.extend(lint_phase(p, artifact_names))
            except Exception as exc:
                issues.append(LintIssue("error", p, f"Lint error: {exc}"))

    # ── lint app graph ────────────────────────────────────────────────────────
    app_md = app_dir / "skill.md"
    if app_md.exists():
        try:
            issues.extend(lint_skill(app_md, artifact_names))
        except Exception as exc:
            issues.append(LintIssue("error", app_md, f"Lint error: {exc}"))

    return issues
