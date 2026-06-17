"""mcp_install kind handler — install an MCP server from the registry.

Handler logic (one-shot, no sub-phases):
  1. Fetch server.json via RegistryClient
  2. Check runtime command availability (npx / uvx / docker / dnx)
  3. Gate via PermissionResolver.require_mcp_install (ADR-0029)
  4. Prompt for secret env vars via intervention_bus; persist with secrets.store
  5. Write mcp.servers.<name> into the target scope config file
  6. Emit mcp_server_installed event (P6)

Scope → file mapping:
  local   → <project>/reyn.local.yaml
  project → <project>/reyn.yaml
  user    → ~/.reyn/config.yaml

This is a P5 exception: reyn.yaml lives outside the workspace, so the OS
handler writes it directly (same pattern as `reyn config set`). The action
is gated behind require_mcp_install permission (ADR-0029) and recorded via
event (P6), which preserves the audit trail.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from reyn.schemas.models import MCPInstallIROp

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx

# ---------------------------------------------------------------------------
# Runtime-hint → command name
# ---------------------------------------------------------------------------

_RUNTIME_CMD: dict[str, str] = {
    "npx":    "npx",
    "uvx":    "uvx",
    "docker": "docker",
    "dnx":    "dnx",
}

_RUNTIME_INSTALL_HINT: dict[str, str] = {
    "npx":    "Node.js が必要です: https://nodejs.org",
    "uvx":    "uv が必要です: https://docs.astral.sh/uv/",
    "docker": "Docker が必要です: https://docs.docker.com/get-docker/",
    "dnx":    ".NET SDK が必要です: https://dotnet.microsoft.com/download",
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _short_name(server_id: str) -> str:
    """Derive a short config key from a registry server_id.

    'io.github.modelcontextprotocol/server-filesystem' → 'server-filesystem'
    'ai.smithery/smithery-ai-slack'                     → 'smithery-ai-slack'
    """
    # Take the part after the last '/' if present; otherwise use the full id.
    return server_id.split("/")[-1] if "/" in server_id else server_id


def _scope_to_path(scope: str, project_root: Path) -> Path:
    """Resolve the target config file path for the given scope.

    Issue #470 (2026-05-22): dynamic MCP server registry is being
    separated from the static deployment config. New installs write
    to ``.reyn/mcp.yaml`` regardless of ``scope`` — the scope flag
    is retained as a no-op for CLI backward compat (= existing
    ``reyn mcp install --scope X`` invocations don't break) but no
    longer determines the write target.

    Rationale: ``reyn.yaml`` semantics = "edit + restart" should
    apply uniformly across all of its content. ``mcp.servers`` was
    the only field that violated this by being op-mutated at
    runtime — separating it into ``.reyn/mcp.yaml`` purifies the
    invariant and matches the existing pattern where dynamic state
    (= ``.reyn/approvals.yaml``) already lives under ``.reyn/``.

    Backward compat (= preserved by ``_merge`` in config.py): if
    the operator hand-wrote ``mcp.servers`` into reyn.yaml,
    those entries continue to load. New installs land in the new
    location; ``reyn config migrate-mcp`` (= follow-up) provides
    explicit migration.
    """
    # Scope arg ignored — single canonical target for dynamic registry.
    _ = scope  # noqa: F841 — retained on signature for CLI compat
    return project_root / ".reyn" / "mcp.yaml"


def _resolve_write_root(workspace: object) -> Path:
    """#1442 Layer B: the project root the install writes under.

    Resolved from the workspace's CANONICAL ``base_dir`` (the attribute the real
    Workspace exposes and op_runtime/file.py reads), with ``root`` as a fallback
    for the CLI source stub, and cwd as the last resort. The handler previously
    checked only ``root`` — which the real Workspace lacks — so any agent /
    registry install silently fell back to cwd, writing into the wrong tree.
    """
    root = getattr(workspace, "base_dir", None) or getattr(workspace, "root", None)
    return Path(root) if root is not None else Path.cwd()


def _build_server_entry(pkg_raw: dict, env_keys: list[str]) -> dict:
    """Build the mcp.servers.<name> config dict from a ServerPackage raw dict.

    Returns a dict with keys: type, command, args, and optionally env.
    The env field uses ${KEY} references only — values stay in secrets.env.

    issue #318: ``type:`` is ALWAYS written (= matches the loader's
    ``MCPClient`` expectation that ``type`` be one of
    ``stdio | http | sse``). Pre-fix the function omitted the field
    for the default stdio case + wrote a ``transport:`` key (not
    ``type:``) for non-stdio — both produced configs that the loader
    rejected with ``Unsupported MCP server type: None``.
    """
    registry_type = pkg_raw.get("registryType", "").lower()
    identifier = pkg_raw.get("identifier", "")
    version = pkg_raw.get("version", "")
    transport_raw = pkg_raw.get("transport") or {}
    transport_type = transport_raw.get("type", "stdio")

    if registry_type == "npm":
        args = ["-y", identifier]
        if version:
            args = ["-y", f"{identifier}@{version}"]
        entry: dict = {"type": transport_type, "command": "npx", "args": args}
    elif registry_type == "pypi":
        args = [identifier]
        if version:
            args = [f"{identifier}=={version}"]
        entry = {"type": transport_type, "command": "uvx", "args": args}
    elif registry_type == "docker":
        args = ["run", "--rm", "-i", identifier]
        entry = {"type": transport_type, "command": "docker", "args": args}
    elif registry_type == "nuget":
        args = ["tool", "run", identifier]
        entry = {"type": transport_type, "command": "dnx", "args": args}
    else:
        # Unknown registry type: store raw info so user can fix manually
        entry = {"type": transport_type, "command": "", "args": [identifier]}

    if env_keys:
        entry["env"] = {k: f"${{{k}}}" for k in env_keys}

    return entry


def _read_yaml_config(path: Path) -> dict:
    """Read a YAML config file; return {} if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_yaml_config(path: Path, data: dict) -> None:
    """Write a dict as YAML to path, creating parent dirs as needed."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle(
    op: MCPInstallIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute an mcp_install op — register an MCP server from registry or source.

    Two install paths:
      - Registry path (``op.source is None``): fetch server.json from
        registry.modelcontextprotocol.io, then install.
      - Source path (``op.source`` non-empty): resolve metadata from the
        specifier (npm:/pypi:/docker: prefix, or GitHub URL) and skip the
        registry fetch entirely.
    """

    # ── 1. Resolve server metadata ────────────────────────────────────────────
    if op.source:
        # Source-based path: skip registry fetch, resolve from specifier.
        from reyn.core.registry.source_resolver import resolve as _resolve_source

        resolution = _resolve_source(op.source)
        if resolution.error:
            return {
                "kind": "mcp_install",
                "status": "error",
                "server_id": op.server_id,
                "source": op.source,
                "error": f"Source resolution failed: {resolution.error}",
            }

        packages_raw = resolution.packages_raw
        remotes_raw = resolution.remotes_raw
        runtime = resolution.runtime_hint
        # Use resolved server_name for the config key; fall back to server_id short name.
        resolved_server_name = resolution.server_name or _short_name(op.server_id or op.source)

    else:
        # Registry path (existing behaviour).
        from reyn.core.registry.client import RegistryClient, RegistryError

        try:
            async with RegistryClient() as client:
                server_json = await client.get_server(op.server_id)
        except RegistryError as exc:
            # #1471: 404 = server not in registry → decision-enabling guidance so
            # the LLM can immediately pivot to mcp__install_package instead of
            # retrying with a registry-only tool.
            if "HTTP 404" in str(exc):
                _err = (
                    f"'{op.server_id}' is not in the official MCP registry. "
                    "For npm / pypi / docker / GitHub packages use "
                    "mcp__install_package(source=<npm|pypi|docker|github>, name=...)."
                )
            else:
                _err = f"Registry fetch failed: {exc}"
            return {
                "kind": "mcp_install",
                "status": "error",
                "server_id": op.server_id,
                "error": _err,
            }

        packages_raw = server_json.raw.get("packages", [])
        remotes_raw = server_json.raw.get("remotes", [])
        runtime = server_json.runtime_hint
        resolved_server_name = _short_name(op.server_id)

    # ── 2. runtimeHint check ──────────────────────────────────────────────────
    if runtime and runtime in _RUNTIME_CMD:
        cmd = _RUNTIME_CMD[runtime]
        if shutil.which(cmd) is None:
            hint = _RUNTIME_INSTALL_HINT.get(runtime, f"'{cmd}' が見つかりません")
            return {
                "kind": "mcp_install",
                "status": "error",
                "server_id": op.server_id,
                "error": f"必要なランタイムが見つかりません: '{cmd}'. {hint}",
            }

    # ── 3. Permission gate (#571 collapse arc Phase 5) ────────────────────────
    # The skill must declare ``file.write: [.reyn/mcp.yaml]`` (= the
    # canonical mutation target) AND ``http.get: [{host:
    # registry.modelcontextprotocol.io}]`` (= the registry the op
    # fetches metadata from) in its frontmatter. Both checks routed
    # through the OS's uniform permission resolver — the bool-axis
    # ``require_mcp_install`` per-server prompt is removed; per-server
    # granularity is enforced at call time via the existing
    # ``permissions.mcp: [<server>]`` gate.
    project_root = _resolve_write_root(ctx.workspace)
    config_path = _scope_to_path(op.scope, project_root)
    if ctx.permission_resolver is not None:
        # #1352-C: thread the agent/operator sandbox policy (SandboxLayer ∩),
        # same as op_runtime/file.py — was missing here, so the write/http gates
        # ran permission-only (SandboxLayer ⊤). None → ⊤ (unchanged).
        _sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.skill_name,
            sandbox_policy=_sandbox,
        )
        # Registry fetch happened via RegistryClient above — gate the
        # host symmetrically so the OS exercises its own permission
        # primitive uniformly. #571 Phase 7: require_http_get is async
        # because the wildcard branch prompts the operator.
        if not op.source:
            await ctx.permission_resolver.require_http_get(
                ctx.permission_decl,
                "registry.modelcontextprotocol.io",
                ctx.intervention_bus,
                ctx.skill_name,
                sandbox_policy=_sandbox,
            )

    # ── 4. Credentials: resolve isSecret env vars from env_overrides or
    # the pre-existing secret store. Two flows depending on caller:
    # (a) ``ctx.intervention_bus is not None`` (= CLI / operator-trusted
    #     entry, StdinInterventionBus or similar): interactively prompt
    #     for each missing secret, persist, continue install.
    # (b) ``ctx.intervention_bus is None`` (= router chat path, #879):
    #     short-circuit with a structured ``needs_secrets`` result so
    #     the LLM guides the operator to ``reyn secret set <KEY>`` and
    #     retries. No mid-flight ask_user from the chat router.
    # #571 Phase 6: every save_secret call routes through
    # require_secret_write against the calling decl.
    from reyn.security.secrets.store import list_secret_keys

    env_overrides = dict(op.env_overrides or {})
    secret_keys_set: list[str] = []
    missing_secret_keys: list[dict] = []
    already_set = set(list_secret_keys())

    def _save_with_gate(key: str, value: str) -> None:
        if ctx.permission_resolver is not None:
            ctx.permission_resolver.require_secret_write(
                ctx.permission_decl, key, ctx.skill_name,
            )
        from reyn.security.secrets.store import save_secret
        save_secret(key, value)

    interactive = ctx.intervention_bus is not None

    for pkg_raw in packages_raw:
        env_vars = pkg_raw.get("environmentVariables", [])
        for ev in env_vars:
            if not isinstance(ev, dict):
                continue
            key = ev.get("name", "")
            if not key:
                continue
            is_secret = ev.get("isSecret", False)
            if not is_secret:
                continue
            if key in env_overrides:
                _save_with_gate(key, env_overrides[key])
                secret_keys_set.append(key)
                continue
            if key in already_set:
                continue
            if interactive:
                from reyn.user_intervention import UserIntervention
                description = ev.get("description", "") or ""
                iv = UserIntervention(
                    kind="mcp_install.secret",
                    prompt=f"環境変数 {key} の値を入力してください",
                    detail=description or (
                        f"{op.server_id} が必要とするシークレット: {key}"
                    ),
                    choices=[],
                )
                answer = await ctx.intervention_bus.request(iv)
                value = getattr(answer, "text", None) or getattr(
                    answer, "choice_id", "",
                )
                if value:
                    _save_with_gate(key, value)
                    secret_keys_set.append(key)
            else:
                missing_secret_keys.append({
                    "name": key,
                    "description": ev.get("description", "") or "",
                })

    if missing_secret_keys:
        missing_names = [k["name"] for k in missing_secret_keys]
        sample_cmd = " && ".join(
            f"reyn secret set {k}" for k in missing_names
        )
        return {
            "kind": "mcp_install",
            "status": "needs_secrets",
            "server_id": op.server_id,
            "missing_secret_keys": missing_secret_keys,
            "guide": (
                "Server requires secret env-vars not yet set: "
                + ", ".join(missing_names)
                + ". Set them via `reyn secret set <KEY>` "
                "(or pass `env_overrides` arg) and retry the install "
                "(mcp__install_registry / mcp__install_package). "
                "Example: " + sample_cmd
            ),
        }

    # ── 5. Write mcp.servers.<name> to scope config file ─────────────────────
    # project_root + config_path already resolved at the permission gate above.
    existing = _read_yaml_config(config_path)

    # Server name: use resolved_server_name (idempotent on re-install)
    server_name = resolved_server_name

    # Build the server entry from the first package
    server_entry: dict = {}
    if packages_raw:
        server_entry = _build_server_entry(packages_raw[0], secret_keys_set)
    elif remotes_raw:
        # Remote (HTTP) server — use remotes[0] URL
        r = remotes_raw[0]
        server_entry = {
            "type": r.get("type", "streamable-http"),
            "url": r.get("url", ""),
        }
        if secret_keys_set:
            server_entry["env"] = {k: f"${{{k}}}" for k in secret_keys_set}

    # Append user-supplied extra args (e.g. ["--server", "pyright"])
    if op.extra_args:
        existing_args = server_entry.get("args", [])
        server_entry["args"] = list(existing_args) + list(op.extra_args)

    # Guard: if runtime detection failed (unknown GitHub URL with no npm/pypi
    # package list), server_entry has neither command nor url — writing an
    # empty entry would silently create a broken config. Fail loud instead.
    if not server_entry.get("command") and not server_entry.get("url"):
        return {
            "kind": "mcp_install",
            "status": "error",
            "source": op.source or "",
            "error": (
                f"GitHub URL '{op.source}' のランタイムを自動判定できませんでした。"
                " npm: / pypi: / docker: prefix を明示するか、"
                " mcp__install_local で command/args を直接指定してください。"
            ),
        }

    # Merge into existing config
    if "mcp" not in existing or not isinstance(existing.get("mcp"), dict):
        existing["mcp"] = {}
    if "servers" not in existing["mcp"] or not isinstance(existing["mcp"].get("servers"), dict):
        existing["mcp"]["servers"] = {}
    existing["mcp"]["servers"][server_name] = server_entry

    _write_yaml_config(config_path, existing)

    installed_path = str(config_path)

    # ── 6. Emit mcp_server_installed event (P6) ───────────────────────────────
    ctx.events.emit(
        "mcp_server_installed",
        server_id=op.server_id,
        server_name=server_name,
        scope=op.scope,
        runtime=runtime or "unknown",
        env_keys_set=secret_keys_set,
        installed_path=installed_path,
        source=op.source or "",
        # NOTE: env values are NOT emitted — only key names for audit
    )

    return {
        "kind": "mcp_install",
        "status": "ok",
        "server_id": op.server_id,
        "server_name": server_name,
        "scope": op.scope,
        "installed_path": installed_path,
        "runtime": runtime or "unknown",
        "env_keys_set": secret_keys_set,
        "source": op.source or "",
    }


register("mcp_install", handle)
