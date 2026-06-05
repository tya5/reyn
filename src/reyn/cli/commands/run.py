"""`reyn run` — execute an app."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reyn.llm.llm import run_async

from ..common_args import add_common_args
from ..logger_factory import make_logger
from ..session import Session
from ..skill_loader import load_skill_from_args
from ..summary import print_run_result


def register(sub) -> None:
    p = sub.add_parser("run", help="Run a skill")
    p.add_argument(
        "skill_name", nargs="?", default=None, metavar="SKILL",
        help=(
            "Skill name to resolve automatically. "
            "Search order: reyn/project/ → reyn/local/ → stdlib. "
            "Example: reyn run skill_builder 'describe your skill'"
        ),
    )
    p.add_argument(
        "--skill-path", default=None, dest="skill_path", metavar="DIR",
        help=(
            "Path to a skill directory containing skill.md "
            "(e.g. reyn/project/my_skill or reyn/local/my_skill). "
            "Use this to point to an explicit location instead of name resolution."
        ),
    )
    p.add_argument(
        "--module", default=None, metavar="MODULE",
        help="Python module path exposing a 'skill' object (e.g. examples.writing_skill.skill)",
    )
    p.add_argument(
        "--skill-root", default=None, dest="skill_root", metavar="DIR",
        help=(
            "Root of the skill tree for shared artifact/phase resolution. "
            "Inferred automatically when using app name or --app-path. "
            "Override with this flag when the inferred root is wrong."
        ),
    )
    p.add_argument(
        "input", nargs="?", default=None, metavar="INPUT",
        help=(
            "Initial input: JSON artifact string or natural language "
            "(auto-wrapped as user_message). "
            "Reads from stdin if omitted (pipe or redirect)."
        ),
    )
    add_common_args(p)
    p.add_argument("--events", action="store_true",
                   help="Print the full event log after execution")
    p.add_argument("--strict", action="store_true",
                   help=(
                       "Enable strict schema validation: enforce required fields at every nesting depth. "
                       "Default (lenient) only enforces required at the top level of each artifact."
                   ))
    p.add_argument("--allow-shell", dest="allow_shell", action="store_true",
                   help=(
                       "Enable the 'shell' Control IR op, which allows the LLM to execute shell commands. "
                       "Required for meta-apps that invoke sub-processes (e.g. app_improver). "
                       "Off by default for safety."
                   ))
    # FP-0014: --allow-untrusted-python renamed → --allow-unsafe-python.
    # Both flags target the same dest so legacy invocations keep working
    # during the Track A → B transition. New code should use the new flag.
    p.add_argument("--allow-unsafe-python", "--allow-untrusted-python",
                   dest="allow_unsafe_python",
                   action="store_true",
                   help=(
                       "Enable unsafe-mode Python preprocessor steps (no AST sandboxing). "
                       "Safe-mode python steps run without this flag. Off by default."
                   ))
    # FP-0008 #1115 Stage 2: route the repo filesystem + command execution
    # through a container EnvironmentBackend instead of the host. Generic (any
    # skill); the OS stays on the host, only the repo FS + exec cross into the
    # container. Used e.g. for faithful in-container benchmarking.
    p.add_argument("--env-backend", dest="env_backend",
                   choices=["host", "docker"], default="host",
                   help=(
                       "Where the repo filesystem and commands execute: 'host' "
                       "(default, no isolation change) or 'docker'. For docker, "
                       "either attach to a running container (--container/--repo-dir) "
                       "or, if --container is omitted, reyn LAUNCHES a mount-mode "
                       "container (workspace bind-mounted, security-hardened)."
                   ))
    p.add_argument("--container", dest="container", default=None, metavar="NAME",
                   help=(
                       "Running container name/id to ATTACH to (--env-backend=docker). "
                       "Omit to have reyn launch a new mount-mode container instead."
                   ))
    p.add_argument("--repo-dir", dest="repo_dir", default=None, metavar="PATH",
                   help=(
                       "In-container absolute path of the repo working tree "
                       "(e.g. /repo) for --env-backend=docker ATTACH mode. Relative "
                       "paths resolve against this; commands run with it as cwd."
                   ))
    p.add_argument("--image", dest="image", default=None, metavar="IMAGE",
                   help=(
                       "Container image for --env-backend=docker LAUNCH mode "
                       "(when --container is omitted). Defaults to a sensible "
                       "generic base; override per task."
                   ))
    p.add_argument("--mount", dest="mounts", action="append", default=None,
                   metavar="host:container[:rw|ro]",
                   help=(
                       "Additional bind mount for docker LAUNCH mode (repeatable). "
                       "The workspace root is always mounted at /workspace."
                   ))
    p.add_argument("--keep-container", dest="keep_container", action="store_true",
                   help=(
                       "Do not tear down a reyn-launched container after the run "
                       "(persistent reuse). Default: launched containers are removed."
                   ))
    p.add_argument("--state-dir", dest="state_dir", default=None, metavar="PATH",
                   help=(
                       "Host-side directory for OS state/artifacts (events, "
                       "offload, artifact handles), kept off the routed repo FS. "
                       "Recommended with --env-backend=docker so state stays on the host."
                   ))
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    session = Session.from_args(args)
    from reyn.cli.credentials_check import verify_credentials_or_exit
    verify_credentials_or_exit(session, args)
    loaded = load_skill_from_args(args)

    raw_input = _read_input(args)
    initial_input = _parse_cli_input(
        raw_input, default_type=_entry_input_type(loaded.skill),
    )

    model, resolved_model = session.model_for(args)
    # output_language is Optional[str]. None propagates all the way down
    # to the LLM prompt builders, which skip the language directive so
    # the LLM picks the reply language based on the user's input
    # naturally. Reyn explicitly does not fall back to a regional default
    # (= no silent "ja" default) — the project targets a global audience.
    output_language = session.output_language_for(args)
    shell_allowed = session.shell_allowed_for(args)
    safety = session.safety_for(args)

    unsafe_python = bool(getattr(args, "allow_unsafe_python", False))
    # Stdlib skills ship with the Reyn team's code and are trusted by
    # construction — auto-allow their unsafe python steps so users don't need
    # --allow-unsafe-python when running e.g. `reyn run mcp_install`.
    if not unsafe_python and loaded.skill_md is not None:
        from reyn.skill.skill_paths import is_stdlib_skill
        unsafe_python = is_stdlib_skill(loaded.skill_md.parent)
    from reyn.agent import Agent
    from reyn.config import _find_project_root, load_project_context
    from reyn.user_intervention import StdinInterventionBus
    project_root = _find_project_root(Path.cwd())
    project_context = load_project_context(session.config, project_root)
    logger = make_logger()
    env_backend, ws_base_dir, ws_state_dir, env_cleanup = _build_environment_backend(args)
    # #997 dir2: the permission/runtime bundle (permission_resolver, mcp_servers,
    # python_allowed_modules, prompt_cache_enabled, sandbox_config, resolver) is
    # derived from config inside Agent.from_config — a caller cannot omit it (the
    # FP-0008 / #1133 wiring-gap class). Only the per-invocation overrides
    # (args-aware safety, the docker env backend, workspace dirs, logger) are
    # passed here.
    agent = Agent.from_config(
        session.config,
        shell_allowed=shell_allowed,
        model=model,
        safety=safety,
        resolver=session.resolver,
        unsafe_python=unsafe_python,
        strict=args.strict,
        subscribers=[logger],
        intervention_bus=StdinInterventionBus(),
        project_context=project_context,
        caller="direct",
        # FP-0008 #1115 Stage 2: inject the SAME container backend instance at
        # both seams (FS = environment_backend, exec = sandbox_backend), agent-
        # level uniform. None (host) preserves the default identity behavior.
        environment_backend=env_backend,
        sandbox_backend=env_backend,
        workspace_base_dir=ws_base_dir,
        workspace_state_dir=ws_state_dir,
    )

    input_type = initial_input.get("type", "unknown")
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model
    print(f"skill           : {loaded.skill.name}")
    print(f"model           : {model_display}")
    print(f"output_language : {output_language}")
    print(f"input type      : {input_type}")
    print(f"input           : {json.dumps(initial_input, ensure_ascii=False)}")
    print()

    try:
        result = run_async(
            agent.run(loaded.skill, initial_input, output_language=output_language)
        )
    except Exception as e:
        print(f"\nError during execution: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Tear down a reyn-launched mount-mode container (no-op for host /
        # attach / --keep-container). Best-effort: a teardown failure must not
        # mask the run outcome.
        if env_cleanup is not None:
            try:
                env_cleanup()
            except Exception as cleanup_exc:  # noqa: BLE001
                print(
                    f"\nWarning: container teardown failed: {cleanup_exc}",
                    file=sys.stderr,
                )

    print()
    if not result.ok:
        print(f"=== Warning: workflow ended with status '{result.status}' ===",
              file=sys.stderr)
    print("=== Final Output ===")
    print(json.dumps(result.data, indent=2, ensure_ascii=False))
    print_run_result(result.token_usage, result.cost_usd)
    print(f"\nevents saved → {agent.events_path}")

    if args.events:
        print()
        _print_events(agent)

    if not result.ok:
        sys.exit(2)


def _build_environment_backend(args: argparse.Namespace, *, launcher=None):
    """Build the EnvironmentBackend + workspace dirs + optional cleanup.

    Returns ``(backend, workspace_base_dir, workspace_state_dir, cleanup)``:

    - host (default): ``(None, None, None, None)`` — identity HostBackend.
    - docker ATTACH (``--container`` given): a ``DockerEnvironmentBackend`` over
      the existing container; base_dir = the in-container ``--repo-dir``; no
      cleanup (the operator owns the container).
    - docker LAUNCH (``--container`` omitted): reyn launches a mount-mode
      container (workspace bind-mounted at ``/workspace``, security-hardened);
      base_dir = ``/workspace``; ``cleanup`` tears it down after the run unless
      ``--keep-container``.

    Generic — no skill-specific knowledge (P7). Image / mount / security are
    agent-level operator config (#1326), independent of phase sandbox-policy.
    """
    backend_kind = getattr(args, "env_backend", "host")
    if backend_kind == "host":
        return None, None, None, None

    if backend_kind == "docker":
        from reyn.environment import DockerEnvironmentBackend
        container = getattr(args, "container", None)
        state_dir = getattr(args, "state_dir", None)
        state_path = Path(state_dir) if state_dir else None

        if container:
            # ATTACH mode: existing operator-owned container (no teardown).
            repo_dir = getattr(args, "repo_dir", None)
            if not repo_dir:
                print(
                    "Error: --env-backend=docker with --container requires --repo-dir.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if state_path is None:
                # baked-repo model: no bind mount, so OS state cannot be made
                # coherent with the in-container repo FS. Warn that state will
                # land on the repo FS (lost on container death) unless host-side.
                print(
                    "Warning: --env-backend=docker --container without --state-dir: "
                    "OS state (events/artifacts) will live on the in-container repo "
                    "FS and is lost on container death. Pass --state-dir for a "
                    "host-side state directory.",
                    file=sys.stderr,
                )
            backend = DockerEnvironmentBackend(container=container, repo_dir=repo_dir)
            return backend, Path(repo_dir), state_path, None

        # LAUNCH mode: reyn starts a mount-mode container (#1324).
        from reyn.config import _find_project_root
        from reyn.environment.container_launcher import (
            DEFAULT_IMAGE,
            WORKSPACE_DEST_DEFAULT,
            ContainerLauncher,
            LaunchConfig,
            parse_mount_spec,
        )
        # _find_project_root returns None when no reyn.yaml is found up the tree;
        # fall back to cwd so the mount source + state_dir are a real host path
        # (NOT the bogus "None/.reyn" that str(None) would produce). The global
        # cwd-vs-project_root resolution refinement is #1316 (separate slice).
        project_root = _find_project_root(Path.cwd())
        workspace_root = str(project_root if project_root is not None else Path.cwd())
        try:
            mounts = [parse_mount_spec(m) for m in (getattr(args, "mounts", None) or [])]
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        keep = bool(getattr(args, "keep_container", False))
        config = LaunchConfig(
            workspace_root=workspace_root,
            image=getattr(args, "image", None) or DEFAULT_IMAGE,
            mounts=mounts,
            persistent=keep,
        )
        launcher = launcher or ContainerLauncher()
        try:
            container_id = launcher.launch(config)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"launched container : {container_id} (image={config.image})")
        backend = DockerEnvironmentBackend(
            container=container_id, repo_dir=WORKSPACE_DEST_DEFAULT
        )
        cleanup = None if keep else (lambda: launcher.teardown(container_id))
        # part2 (slice-2): in the workspace-mount model the host workspace_root is
        # bind-mounted at /workspace, so the OS state dir defaults to the HOST
        # workspace_root/.reyn — that same dir appears in-container at
        # /workspace/.reyn, keeping OS index/approvals and the agent FS coherent
        # (the bind-mount's purpose). An explicit --state-dir still wins.
        launch_state_path = state_path or (Path(workspace_root) / ".reyn")
        return backend, Path(WORKSPACE_DEST_DEFAULT), launch_state_path, cleanup

    # argparse `choices` prevents this, but stay defensive.
    print(f"Error: unknown --env-backend '{backend_kind}'.", file=sys.stderr)
    sys.exit(1)


def _read_input(args: argparse.Namespace) -> str:
    if args.input is not None:
        return args.input
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("Error: provide INPUT argument or pipe input via stdin.", file=sys.stderr)
    sys.exit(1)


def _entry_input_type(skill) -> str | None:
    """Return the artifact type name to wrap a bare-data CLI input dict with,
    or None when the wrap isn't safe to apply automatically.

    Safe to wrap when the entry phase has exactly one input artifact (no
    ``anyOf`` union) and that artifact is declared ``wrapped: true`` (default)
    so its compiled schema carries ``{type, data}`` envelope properties.

    Skipped when:
      - entry phase missing (defensive)
      - input is an ``anyOf`` of multiple artifact types (= which type would
        we wrap with? — leave to the user)
      - artifact is declared ``wrapped: false`` (= no envelope; the bare dict
        IS the artifact, not its inner data)
    """
    entry = skill.phases.get(skill.entry_phase)
    if entry is None:
        return None
    schema = entry.input_schema or {}
    if "anyOf" in schema:
        return None
    props = schema.get("properties") or {}
    if "type" not in props or "data" not in props:
        return None
    return entry.input_schema_name


def _parse_cli_input(raw: str, *, default_type: str | None = None) -> dict:
    """Accept JSON or natural language and return a Reyn artifact dict.

    Cases:
      - **Non-JSON text** → wrapped as ``{type: user_message, data: {text}}``.
      - **JSON dict already in envelope form** (= has a ``type`` key) →
        passed through unchanged.
      - **JSON dict in bare-data form + ``default_type`` provided** →
        auto-wrapped as ``{type: default_type, data: <parsed>}``. This is
        the path that lets ``reyn run index_docs '{"source":..., "path":...}'``
        (README's RAG demo) work without forcing users to know the artifact
        envelope; the skill loader supplies the target type via
        ``_entry_input_type``.
      - **Anything else** (JSON list, string, number, dict with no
        ``default_type``) → passed through unchanged; downstream artifact
        validation reports the mismatch.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "user_message", "data": {"text": raw}}

    if (
        isinstance(parsed, dict)
        and "type" not in parsed
        and default_type is not None
    ):
        return {"type": default_type, "data": parsed}
    return parsed


def _build_permission_resolver(config, shell_allowed: bool, unsafe_python: bool = False):
    from reyn.config import _find_project_root
    from reyn.permissions.permissions import PermissionResolver
    project_root = _find_project_root(Path.cwd())
    perm_config = getattr(config, "permissions", {}) or {}
    if shell_allowed and "shell" not in perm_config:
        perm_config = dict(perm_config, shell="allow")
    return PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
        unsafe_python_allowed=unsafe_python,
    )


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))
