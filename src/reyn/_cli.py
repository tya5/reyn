import argparse
import importlib
import json
import sys
from pathlib import Path


def _load_config():
    from reyn.config import load_config
    return load_config()


def _apply_config_env(config) -> None:
    """Set env vars from config so all litellm calls pick them up automatically.
    API keys are intentionally excluded — set OPENAI_API_KEY / ANTHROPIC_API_KEY etc. in your shell.
    """
    import os
    if config.api_base:
        os.environ.setdefault("LITELLM_API_BASE", config.api_base)


def _make_resolver(config):
    from reyn.model_resolver import ModelResolver
    return ModelResolver(config.models)


def _parse_cli_input(raw: str) -> dict:
    """Accept JSON or natural language. Natural language → user_message artifact."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "user_message", "data": {"text": raw}}


def _resolve_app_name(name: str) -> tuple[Path, Path | None]:
    """Resolve a short app name to (app_dir, dsl_root).

    Search order: reyn/local/ → reyn/project/ → stdlib.
    Returns (app_dir, dsl_root) where dsl_root is None when it cannot be inferred.
    Exits with an error message if not found.
    """
    stdlib_root = Path(__file__).parent.parent / "stdlib"
    candidates: list[tuple[Path, Path]] = [
        (Path("reyn") / "local" / name,   Path("reyn")),
        (Path("reyn") / "project" / name, Path("reyn")),
        (stdlib_root / "apps" / name,      stdlib_root),
    ]
    for app_dir, dsl_root in candidates:
        if (app_dir / "app.md").exists():
            return app_dir, dsl_root
    checked = "\n  ".join(str(d / "app.md") for d, _ in candidates)
    print(f"Error: app '{name}' not found. Looked in:\n  {checked}", file=sys.stderr)
    sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    config = _load_config()
    _apply_config_env(config)
    resolver = _make_resolver(config)

    if args.app_path:
        app_dir = Path(args.app_path)
        app_md = app_dir / "app.md"
        dsl_root = args.dsl_root
        try:
            from reyn.compiler import load_dsl_app
            app = load_dsl_app(str(app_md), dsl_root=dsl_root)
        except Exception as e:
            print(f"Error: failed to compile DSL '{app_md}': {e}", file=sys.stderr)
            sys.exit(1)
    elif args.app_name:
        app_dir, inferred_root = _resolve_app_name(args.app_name)
        dsl_root = args.dsl_root or str(inferred_root)
        app_md = app_dir / "app.md"
        print(f"resolved        : {app_md}  (dsl-root: {dsl_root})")
        try:
            from reyn.compiler import load_dsl_app
            app = load_dsl_app(str(app_md), dsl_root=dsl_root)
        except Exception as e:
            print(f"Error: failed to compile DSL '{app_md}': {e}", file=sys.stderr)
            sys.exit(1)
    elif args.module:
        try:
            module = importlib.import_module(args.module)
        except ModuleNotFoundError as e:
            print(f"Error: cannot import module '{args.module}': {e}", file=sys.stderr)
            sys.exit(1)
        if not hasattr(module, "app"):
            print(f"Error: module '{args.module}' has no 'app' attribute.", file=sys.stderr)
            sys.exit(1)
        app = module.app
    else:
        print("Error: provide an app name (positional), --app-path DIR, or --module.", file=sys.stderr)
        sys.exit(1)

    if args.input is not None:
        raw_input = args.input
    elif not sys.stdin.isatty():
        raw_input = sys.stdin.read().strip()
    else:
        print("Error: provide INPUT argument or pipe input via stdin.", file=sys.stderr)
        sys.exit(1)

    initial_input = _parse_cli_input(raw_input)

    model = args.model or config.model
    output_language = args.output_language or config.output_language
    shell_allowed = args.allow_shell or config.shell_allowed
    max_phase_visits = args.max_phase_visits if args.max_phase_visits is not None else config.max_phase_visits

    resolved_model = resolver.resolve(model)

    from reyn.agent import Agent
    from reyn.permissions import PermissionResolver
    from reyn.config import _find_project_root
    project_root = _find_project_root(Path.cwd())
    perm_config = getattr(config, "permissions", {}) or {}
    # backward compat: if global shell_allowed, pre-approve shell in permissions
    if shell_allowed and "shell" not in perm_config:
        perm_config = dict(perm_config, shell="allow")
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
    )

    if args.rich:
        from reyn.reporters.rich import RichLogger
        logger = RichLogger()
    else:
        from reyn.reporters.console import ConsoleLogger
        logger = ConsoleLogger()

    agent = Agent(
        model=model,
        state_dir=config.state_dir,
        strict=args.strict,
        subscribers=[logger],
        shell_allowed=shell_allowed,
        resolver=resolver,
        permission_resolver=perm_resolver,
        max_phase_visits=max_phase_visits,
    )

    input_type = initial_input.get("type", "unknown")
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model
    print(f"app             : {app.name}")
    print(f"model           : {model_display}")
    print(f"output_language : {output_language}")
    print(f"input type      : {input_type}")
    print(f"input           : {json.dumps(initial_input, ensure_ascii=False)}")
    print()

    try:
        result = agent.run(app, initial_input, output_language=output_language)
    except Exception as e:
        print(f"\nError during execution: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    if not result.ok:
        print(f"=== Warning: workflow ended with status '{result.status}' ===",
              file=sys.stderr)
    print("=== Final Output ===")
    print(json.dumps(result.data, indent=2, ensure_ascii=False))
    if result.token_usage:
        u = result.token_usage
        print(f"\ntokens: {u.prompt_tokens:,} prompt + {u.completion_tokens:,} completion"
              f" = {u.total_tokens:,} total")
    print(f"\nevents saved → {agent.events_path}")

    if args.events:
        print()
        _print_events(agent)

    if not result.ok:
        sys.exit(2)


def cmd_apps(args: argparse.Namespace) -> None:
    import yaml
    from .compiler.parser import _split_frontmatter

    stdlib_root = Path(__file__).parent.parent / "stdlib"

    search_roots: list[tuple[str, Path]] = [
        ("project", Path("reyn") / "project"),
        ("local",   Path("reyn") / "local"),
        ("stdlib",  stdlib_root / "apps"),
    ]

    def _read_app_md(app_md: Path) -> tuple[dict, str]:
        try:
            fm, body = _split_frontmatter(app_md.read_text(encoding="utf-8"))
            return fm, body
        except Exception:
            return {}, ""

    def _find_app(name: str) -> Path | None:
        for _, apps_dir in search_roots:
            candidate = apps_dir / name / "app.md"
            if candidate.exists():
                return candidate
        return None

    # Detail view: reyn apps <name>
    if getattr(args, "app_name", None):
        app_md = _find_app(args.app_name)
        if app_md is None:
            print(f"App '{args.app_name}' not found.")
            return
        fm, body = _read_app_md(app_md)
        print(f"\n{fm.get('name', args.app_name)}")
        if fm.get("description"):
            print(f"{fm['description']}\n")
        if body.strip():
            print(body.strip())
        else:
            print("(no documentation)")
        print()
        return

    # List view: reyn apps
    found_any = False
    seen: set[str] = set()

    for label, apps_dir in search_roots:
        if not apps_dir.exists():
            continue
        entries = sorted(p for p in apps_dir.iterdir() if p.is_dir() and (p / "app.md").exists())
        if not entries:
            continue
        print(f"\n{label}  ({apps_dir})")
        for app_dir in entries:
            name = app_dir.name
            fm, _ = _read_app_md(app_dir / "app.md")
            desc = (fm.get("description") or "").strip().splitlines()[0] if fm.get("description") else ""
            shadowed = " [shadowed]" if name in seen else ""
            desc_str = f"  — {desc}" if desc else ""
            print(f"  {name}{desc_str}{shadowed}")
            seen.add(name)
        found_any = True

    if not found_any:
        print("No apps found.")
    print()
    print("Run 'reyn apps <name>' for usage details.")


def cmd_events(args: argparse.Namespace) -> None:
    import json
    from pathlib import Path
    from reyn.models import Event

    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    conversation: bool = args.conversation
    if args.rich:
        from reyn.reporters.rich import RichLogger
        logger = RichLogger(conversation=conversation)
    else:
        from reyn.reporters.console import ConsoleLogger
        logger = ConsoleLogger(conversation=conversation)

    filter_types: set[str] = set(args.filter_types)
    skip_types: set[str] = set(args.skip_types)
    if conversation:
        # Show only LLM conversation events; ignore explicit filter/skip when --conversation given
        filter_types = {"context_built", "llm_response_received"}

    count = 0
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[line {lineno}] JSON parse error: {e}", file=sys.stderr)
                continue

            event_type = raw.get("type", "")
            if filter_types and event_type not in filter_types:
                continue
            if event_type in skip_types:
                continue

            try:
                event = Event.model_validate(raw)
            except Exception as e:
                print(f"[line {lineno}] Event parse error: {e}", file=sys.stderr)
                continue

            logger(event)
            count += 1

    print(f"\n({count} events replayed from {path})")


def cmd_eval(args: argparse.Namespace) -> None:
    import json
    from datetime import datetime, timezone
    from reyn.compiler.eval_loader import load_eval_spec
    from reyn.compiler import load_dsl_app
    from reyn.pricing import TokenUsage

    config = _load_config()
    _apply_config_env(config)
    resolver = _make_resolver(config)

    try:
        spec = load_eval_spec(args.spec)
    except Exception as e:
        print(f"Error loading eval spec: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve target app: bare name → keep as-is for sub-runner resolution;
    # path → use directly (load_dsl_app infers dsl_root from path hierarchy)
    app_ref = spec.app_dsl_path
    if "/" not in app_ref and not app_ref.endswith(".md"):
        app_dir, inferred_root = _resolve_app_name(app_ref)
        target_app_path = str(app_dir / "app.md")
        target_dsl_root = args.dsl_root or str(inferred_root)
        print(f"resolved        : {target_app_path}  (dsl-root: {target_dsl_root})")
    else:
        target_app_path = app_ref
        target_dsl_root = spec.dsl_root or args.dsl_root

    # Load the eval stdlib app
    stdlib_root = Path(__file__).parent.parent / "stdlib"
    eval_app_md = stdlib_root / "apps" / "eval" / "app.md"
    try:
        eval_app = load_dsl_app(str(eval_app_md), dsl_root=str(stdlib_root))
    except Exception as e:
        print(f"Error loading eval stdlib app: {e}", file=sys.stderr)
        sys.exit(1)

    model = args.model or spec.model or config.model
    output_language = args.output_language or config.output_language
    resolved_model = resolver.resolve(model)
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model

    print(f"=== Eval: {app_ref}  [{len(spec.cases)} case(s)] ===")
    print(f"    model={model_display}")
    print()

    from reyn.agent import Agent

    case_results: list[dict] = []
    total_tokens = TokenUsage()

    for case in spec.cases:
        print(f"━━━ case: {case.name} ━━━")
        print(f"  input: {case.input[:120]}")

        # Build phase_criteria for eval_case_input; skip "final" (not supported by eval app)
        phase_criteria = [
            {
                "phase_name": pc.phase,
                "criteria": [
                    {"description": qc.text}
                    if qc.tag != "aspirational"
                    else {"description": qc.text, "required": False}
                    for qc in pc.criteria
                ],
            }
            for pc in case.phase_criteria
            if pc.phase is not None
        ]

        input_artifact: dict = {
            "type": "eval_case_input",
            "data": {
                "case_name": case.name,
                "case_input": case.input,
                "spec_path": args.spec,
                "target_app_path": target_app_path,
                "phase_criteria": phase_criteria,
            },
        }
        if target_dsl_root:
            input_artifact["data"]["dsl_root"] = target_dsl_root

        max_phase_visits = (
            args.max_phase_visits if args.max_phase_visits is not None
            else config.max_phase_visits
        )
        agent = Agent(
            model=model,
            state_dir=config.state_dir,
            resolver=resolver,
            max_phase_visits=max_phase_visits,
        )

        try:
            result = agent.run(eval_app, input_artifact, output_language=output_language)
        except Exception as e:
            print(f"  Error: {e}", file=sys.stderr)
            case_results.append({
                "case_name": case.name,
                "passed": False,
                "overall_score": 0.0,
                "passed_criteria": 0,
                "total_criteria": 0,
                "weakest_phase": "",
                "summary": str(e),
            })
            print()
            continue

        data = result.data
        if result.token_usage:
            total_tokens = total_tokens + result.token_usage

        passed_sym = "✓" if data.get("passed") else "✗"
        score = data.get("overall_score", 0.0)
        pc_count = data.get("passed_criteria", 0)
        tc_count = data.get("total_criteria", 0)
        print(f"  {passed_sym} score={score:.2f}  ({pc_count}/{tc_count} required)")
        if data.get("weakest_phase"):
            print(f"  weakest: {data['weakest_phase']}")
        if data.get("summary"):
            print(f"  {data['summary']}")

        case_results.append({"case_name": case.name, **data})
        print()

    # Overall summary
    all_passed = all(r.get("passed") for r in case_results)
    passed_count = sum(1 for r in case_results if r.get("passed"))
    overall_sym = "✓" if all_passed else "✗"
    print(f"{'═' * 55}")
    print(f" {overall_sym} {passed_count}/{len(case_results)} cases passed")
    if total_tokens.total_tokens > 0:
        u = total_tokens
        print(f" tokens: {u.prompt_tokens:,} prompt + {u.completion_tokens:,} completion"
              f" = {u.total_tokens:,} total")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    eval_dir = Path(config.state_dir) / "evals"
    eval_dir.mkdir(parents=True, exist_ok=True)
    app_name = Path(target_app_path).parent.name
    result_path = eval_dir / f"{ts}_{app_name}.json"
    result_path.write_text(
        json.dumps({
            "spec_path": args.spec,
            "app": app_ref,
            "model": resolved_model,
            "timestamp": ts,
            "cases": case_results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f" Results → {result_path}")
    print(f"{'═' * 55}")

    if not all_passed:
        sys.exit(2)


def cmd_lint(args: argparse.Namespace) -> None:
    from reyn.compiler.linter import lint_app_dir

    app_dir, _ = _resolve_app_name(args.app)
    issues = lint_app_dir(app_dir)

    if not issues:
        print("No issues found.")
        return

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for issue in issues:
        print(issue)

    print()
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")

    if errors:
        sys.exit(1)


def cmd_format(args: argparse.Namespace) -> None:
    from pathlib import Path
    from reyn.compiler.formatter import format_dsl

    dsl_root = Path(args.dsl)
    check_only = args.check

    changed = format_dsl(dsl_root, write=not check_only)

    if not changed:
        print("All files are already formatted.")
        return

    verb = "Would reformat" if check_only else "Reformatted"
    for p in changed:
        print(f"{verb}: {p}")

    if check_only:
        print(f"\n{len(changed)} file(s) would be reformatted.")
        sys.exit(1)
    else:
        print(f"\n{len(changed)} file(s) reformatted.")


_REYN_YAML_TEMPLATE = """\
# Reyn project configuration — commit this file.
# Local overrides belong in .reyn/config.yaml (gitignored) — never commit secrets here.

# Default model class when --model is not specified.
model: standard

# Model class → LiteLLM model string.
# Three standard tiers. Edit to match your provider.
models:
  light:    openai/gpt-4o-mini
  standard: openai/gpt-4o
  strong:   openai/gpt-4o

# output_language: en          # en | ja | zh | ...
# shell_allowed: false         # allow 'shell' Control IR op (meta-apps only)
"""

_REYN_LOCAL_CONFIG_TEMPLATE = """\
# Local environment overrides — gitignored, never commit.

# LiteLLM proxy base URL (omit if calling providers directly)
# api_base: http://localhost:4000

# API keys must be set as environment variables, not here:
#   export OPENAI_API_KEY=sk-...
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export GEMINI_API_KEY=...

# Override model mappings for your local setup (optional)
# models:
#   light:    openai/gemini-2.5-flash-lite
#   standard: openai/gemini-2.5-flash-lite
#   strong:   openai/gemini-2.5-flash-lite
"""


def cmd_init(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    created: list[str] = []
    skipped: list[str] = []

    # reyn.yaml
    project_cfg = cwd / "reyn.yaml"
    if project_cfg.exists():
        skipped.append("reyn.yaml")
    else:
        project_cfg.write_text(_REYN_YAML_TEMPLATE, encoding="utf-8")
        created.append("reyn.yaml")

    # .reyn/config.yaml
    reyn_dir = cwd / ".reyn"
    reyn_dir.mkdir(exist_ok=True)
    local_cfg = reyn_dir / "config.yaml"
    if local_cfg.exists():
        skipped.append(".reyn/config.yaml")
    else:
        local_cfg.write_text(_REYN_LOCAL_CONFIG_TEMPLATE, encoding="utf-8")
        created.append(".reyn/config.yaml")

    # .gitignore — add .reyn/ if not already present
    gitignore = cwd / ".gitignore"
    gitignore_note = ""
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".reyn/" not in content:
            gitignore.write_text(content.rstrip() + "\n.reyn/\n", encoding="utf-8")
            gitignore_note = "  (.gitignore updated)"
    else:
        gitignore.write_text(".reyn/\n", encoding="utf-8")
        gitignore_note = "  (.gitignore created)"

    for name in created:
        suffix = gitignore_note if name == ".reyn/config.yaml" else ""
        print(f"  Created   {name}{suffix}")
    for name in skipped:
        print(f"  Exists    {name}  (skipped)")

    print()
    print("Next steps:")
    print("  1. Edit reyn.yaml         — set model mappings for your provider")
    print("  2. Edit .reyn/config.yaml — set api_base if using a proxy")
    print("  3. Export your API key:")
    print("       export OPENAI_API_KEY=sk-...")
    print("       export ANTHROPIC_API_KEY=sk-ant-...")
    print("  4. Run an app:")
    print('       reyn run app_builder "describe the app you want to build"')


_CONFIG_FIELDS = [
    {
        "key":     "model",
        "default": "standard",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Default model class used when a phase has no model_class.",
        "values":  "light | standard | strong  (resolved via models map)",
        "example": "model: standard",
    },
    {
        "key":     "models",
        "default": "{}",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Map of model class names to LiteLLM model strings.",
        "values":  "dict: class_name → litellm_model_string",
        "example": "models:\n  light:    openai/gpt-4o-mini\n  standard: openai/gpt-4o\n  strong:   openai/o3",
    },
    {
        "key":     "api_base",
        "default": "(none)",
        "scope":   ".reyn/config.yaml  (keep out of git)",
        "desc":    "LiteLLM proxy base URL. Set this if you route requests through a local proxy.",
        "values":  "URL string",
        "example": "api_base: http://localhost:4000",
    },
    {
        "key":     "output_language",
        "default": "ja",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Language code injected into the context frame for all LLM outputs.",
        "values":  "BCP-47 language tag (e.g. en, ja, zh)",
        "example": "output_language: en",
    },
    {
        "key":     "state_dir",
        "default": ".reyn",
        "scope":   "reyn.yaml",
        "desc":    "Directory for internal state: artifacts, event logs, eval runs.",
        "values":  "relative or absolute path",
        "example": "state_dir: .reyn",
    },
    {
        "key":     "shell_allowed",
        "default": "false",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Allow the shell Control IR op globally. Equivalent to --allow-shell on every run.",
        "values":  "true | false",
        "example": "shell_allowed: false",
    },
    {
        "key":     "permissions",
        "default": "{}",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Pre-approve specific Control IR ops without interactive prompts.",
        "values":  "dict: op_kind → 'allow'",
        "example": "permissions:\n  shell: allow",
    },
]


def cmd_config(args: argparse.Namespace) -> None:
    sub = getattr(args, "config_cmd", None)
    if sub == "fields":
        _cmd_config_fields()
    elif sub == "show":
        _cmd_config_show()
    elif sub == "get":
        _cmd_config_get(args.key)
    elif sub == "set":
        _cmd_config_set(args.key, args.value)
    else:
        # Default: show
        _cmd_config_show()


def _cmd_config_fields() -> None:
    W_KEY, W_DEF, W_SCOPE = 18, 10, 34
    header = f"{'Field':<{W_KEY}}  {'Default':<{W_DEF}}  {'Scope':<{W_SCOPE}}  Description"
    print(header)
    print("─" * len(header))
    for f in _CONFIG_FIELDS:
        print(f"{f['key']:<{W_KEY}}  {f['default']:<{W_DEF}}  {f['scope']:<{W_SCOPE}}  {f['desc']}")
        print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}  Values:  {f['values']}")
        print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}  Example: {f['example'].splitlines()[0]}")
        for extra_line in f['example'].splitlines()[1:]:
            print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}           {extra_line}")
        print()


def _cmd_config_show() -> None:
    import yaml
    config = _load_config()
    effective = {
        "model":           config.model,
        "models":          config.models,
        "api_base":        config.api_base or "(not set)",
        "output_language": config.output_language,
        "state_dir":       config.state_dir,
        "shell_allowed":   config.shell_allowed,
        "permissions":     config.permissions,
    }
    print("# Effective config (merged from all sources)")
    print(yaml.dump(effective, allow_unicode=True, default_flow_style=False), end="")


def _cmd_config_get(key: str) -> None:
    import yaml
    config = _load_config()
    value = getattr(config, key, None)
    if value is None:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print(f"Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)
    if isinstance(value, (dict, list)):
        print(yaml.dump(value, allow_unicode=True, default_flow_style=False), end="")
    else:
        print(value)


def _cmd_config_set(key: str, value: str) -> None:
    import yaml
    valid_keys = {f["key"] for f in _CONFIG_FIELDS}
    check_key = key.split(".")[0] if "." in key else key
    if check_key not in valid_keys:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print(f"Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)

    local_cfg = Path(".reyn") / "config.yaml"
    local_cfg.parent.mkdir(exist_ok=True)
    current: dict = {}
    if local_cfg.exists():
        current = yaml.safe_load(local_cfg.read_text(encoding="utf-8")) or {}

    # Parse value: try YAML first so booleans/numbers work
    try:
        parsed = yaml.safe_load(value)
    except Exception:
        parsed = value

    # Nested key support: models.standard → models: {standard: ...}
    if "." in key:
        parent, child = key.split(".", 1)
        current.setdefault(parent, {})[child] = parsed
    else:
        current[key] = parsed

    local_cfg.write_text(yaml.dump(current, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    print(f"Set {key} = {parsed!r}  →  {local_cfg}")


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reyn",
        description="Agent OS MVP — LLM-driven phase execution",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    init_p = sub.add_parser("init", help="Create reyn.yaml and .reyn/config.yaml in the current directory")
    init_p.set_defaults(func=cmd_init)

    config_p = sub.add_parser("config", help="View and edit reyn configuration")
    config_sub = config_p.add_subparsers(dest="config_cmd", metavar="<subcommand>")
    config_p.set_defaults(func=cmd_config)

    config_sub.add_parser("show", help="Show current effective config (merged from all sources)")
    config_sub.add_parser("fields", help="List all config fields with descriptions and examples")

    config_get_p = config_sub.add_parser("get", help="Get a single config value")
    config_get_p.add_argument("key", metavar="KEY", help="Config key (e.g. model, api_base)")

    config_set_p = config_sub.add_parser("set", help="Set a config value in .reyn/config.yaml")
    config_set_p.add_argument("key", metavar="KEY",
                              help="Config key (e.g. api_base, models.standard). Run 'reyn config fields' for the full list.")
    config_set_p.add_argument("value", metavar="VALUE", help="Value to set (YAML syntax accepted)")

    apps_p = sub.add_parser("apps", help="List available apps, or show usage details for one app")
    apps_p.add_argument("app_name", nargs="?", default=None, metavar="APP",
                        help="App name to show details for (omit to list all)")
    apps_p.set_defaults(func=cmd_apps)

    run_p = sub.add_parser("run", help="Run an app")
    run_p.add_argument(
        "app_name",
        nargs="?",
        default=None,
        metavar="APP",
        help=(
            "App name to resolve automatically. "
            "Search order: reyn/project/ → reyn/local/ → stdlib. "
            "Example: reyn run app_builder 'describe your app'"
        ),
    )
    run_p.add_argument(
        "--app-path",
        default=None,
        dest="app_path",
        metavar="DIR",
        help=(
            "Path to an app directory containing app.md "
            "(e.g. reyn/project/my_app or reyn/local/my_app). "
            "Use this to point to an explicit location instead of name resolution."
        ),
    )
    run_p.add_argument(
        "--module",
        default=None,
        metavar="MODULE",
        help="Python module path exposing an 'app' object (e.g. examples.writing_app.app)",
    )
    run_p.add_argument(
        "--dsl-root",
        default=None,
        dest="dsl_root",
        metavar="DIR",
        help=(
            "Root of the DSL tree for shared artifact/phase resolution. "
            "Inferred automatically when using app name or --app-path. "
            "Override with this flag when the inferred root is wrong."
        ),
    )
    run_p.add_argument(
        "input",
        nargs="?",
        default=None,
        metavar="INPUT",
        help=(
            "Initial input: JSON artifact string or natural language "
            "(auto-wrapped as user_message). "
            "Reads from stdin if omitted (pipe or redirect)."
        ),
    )
    run_p.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=(
            "Model class name (light/standard/strong) or LiteLLM model string. "
            "Resolved via reyn.yaml models map. "
            "Default: from reyn.yaml 'model' key, or 'standard'."
        ),
    )
    run_p.add_argument(
        "--output-language",
        default=None,
        dest="output_language",
        metavar="LANG",
        help="Output language code (default: from reyn.yaml or ja)",
    )
    run_p.add_argument(
        "--events",
        action="store_true",
        help="Print the full event log after execution",
    )
    run_p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Enable strict schema validation: enforce required fields at every nesting depth. "
            "Default (lenient) only enforces required at the top level of each artifact."
        ),
    )
    run_p.add_argument(
        "--rich",
        action="store_true",
        help="Use Rich-styled console output instead of plain text logging.",
    )
    run_p.add_argument(
        "--allow-shell",
        dest="allow_shell",
        action="store_true",
        help=(
            "Enable the 'shell' Control IR op, which allows the LLM to execute shell commands. "
            "Required for meta-apps that invoke sub-processes (e.g. app_improver). "
            "Off by default for safety."
        ),
    )
    run_p.add_argument(
        "--max-phase-visits",
        dest="max_phase_visits",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum times any single phase may be visited per run (0 = unlimited). "
            "Prevents infinite rollback/revision loops. Default: from reyn.yaml or 25."
        ),
    )
    run_p.set_defaults(func=cmd_run)

    # ── eval ──────────────────────────────────────────────────────────────────
    eval_p = sub.add_parser("eval", help="Run an eval spec against an app")
    eval_p.add_argument("spec", metavar="FILE",
                        help="Path to the eval.md spec file (e.g. reyn/local/my_app/eval.md)")
    eval_p.add_argument("--model", default=None, metavar="MODEL",
                        help="Model class name or LiteLLM string (default: from spec or config)")
    eval_p.add_argument("--dsl-root", dest="dsl_root", default=None, metavar="DIR",
                        help="DSL root override for the target app (default: inferred from path)")
    eval_p.add_argument("--output-language", default=None, dest="output_language", metavar="LANG",
                        help="Output language code (default: from config)")
    eval_p.add_argument(
        "--max-phase-visits", dest="max_phase_visits", type=int, default=None, metavar="N",
        help=(
            "Maximum times any single phase may be visited (cascades to sub-apps via run_app). "
            "Useful for capping rollback loops in target apps. Default: from reyn.yaml or 25."
        ),
    )
    eval_p.set_defaults(func=cmd_eval)

    lint_p = sub.add_parser("lint", help="Lint a DSL app for issues")
    lint_p.add_argument(
        "app",
        metavar="APP",
        help="App name to lint (same resolution as `reyn run <app>`)",
    )
    lint_p.set_defaults(func=cmd_lint)

    fmt_p = sub.add_parser("format", help="Format DSL files into canonical form")
    fmt_p.add_argument(
        "--dsl",
        required=True,
        metavar="DIR",
        help="Root directory of the DSL tree (e.g. dsl/)",
    )
    fmt_p.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: report files that would change without writing them",
    )
    fmt_p.set_defaults(func=cmd_format)

    ev_p = sub.add_parser("events", help="Replay a saved event JSONL file to the console")
    ev_p.add_argument(
        "path",
        metavar="FILE",
        help="Path to the .jsonl event file (e.g. workspace/runs/20260426T…_app_builder.jsonl)",
    )
    ev_p.add_argument(
        "--rich",
        action="store_true",
        help="Use Rich-styled output instead of plain text",
    )
    ev_p.add_argument(
        "--filter",
        metavar="TYPE",
        action="append",
        dest="filter_types",
        default=[],
        help="Only show events of this type (repeatable, e.g. --filter phase_started --filter phase_completed)",
    )
    ev_p.add_argument(
        "--skip",
        metavar="TYPE",
        action="append",
        dest="skip_types",
        default=[],
        help="Skip events of this type (repeatable)",
    )
    ev_p.add_argument(
        "--conversation",
        action="store_true",
        help=(
            "Show LLM conversation history: display context frames sent to the LLM "
            "and the raw responses received, in order. "
            "Overrides --filter and --skip."
        ),
    )
    ev_p.set_defaults(func=cmd_events)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
