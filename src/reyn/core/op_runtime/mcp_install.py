"""mcp_install kind handler — install an MCP server from the registry.

Handler logic (one-shot, no sub-phases):
  1. Fetch server.json via RegistryClient
  2. Check runtime command availability (npx / uvx / docker / dnx)
  3. Gate via PermissionResolver.require_mcp_install (ADR-0029)
  4. Prompt for secret env vars via intervention_bus; persist with secrets.store
  5. Reload (#2761 PR-3): a PURE ADDITION on a live per-session reloader takes the
     IMMEDIATE mid-turn path — PROBE the server (spawn/connect + list_tools) FIRST,
     write mcp.servers.<name> ONLY on a successful probe (probe-then-commit: a
     failed/cancelled probe leaves nothing written — no half-install), then
     apply_now so its tools are resolvable this same turn. A same-name overwrite
     (the documented re-install fix) or no per-session reloader keeps the deferred
     turn-boundary path (write + request_reload), unchanged.
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
from typing import TYPE_CHECKING, Literal

from reyn.schemas.models import MCPInstallIROp

if TYPE_CHECKING:
    import asyncio

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
    "npx":    "Node.js is required: https://nodejs.org",
    "uvx":    "uv is required: https://docs.astral.sh/uv/",
    "docker": "Docker is required: https://docs.docker.com/get-docker/",
    "dnx":    ".NET SDK is required: https://dotnet.microsoft.com/download",
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
    return project_root / ".reyn" / "config" / "mcp.yaml"


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


async def probe_mcp_server(
    server_name: str, server_entry: dict, *, agent_id: str | None = None,
    cancel_event: "asyncio.Event | None" = None,
) -> "str | None":
    """#2761 PR-3: probe a prospective MCP server (spawn/connect + ``list_tools``)
    BEFORE its config is committed — the probe-then-commit atomicity gate.

    Returns ``None`` on a successful probe (the server is reachable and advertises
    tools) or an error string on failure — including a Ctrl-C cancel (#2813; see
    below). The caller writes the config ONLY on ``None`` — so a failed OR
    cancelled probe leaves NOTHING written (no half-install, no rollback).

    Routed through the crash-safe :class:`~reyn.mcp.gateway.MCPGateway` seam (#2421):
    open + list + teardown inside one contain-all boundary + a per-server timeout,
    task-affine so the SDK's stdio_client/ClientSession scopes close in the same task.
    The gateway raises ONLY :class:`MCPFault` for a transport/timeout fault (contained
    here → error string), or PROPAGATES control flow: a genuine Ctrl+C
    ``CancelledError`` (host-task cancel, e.g. ``Session.shutdown``'s hard-cancel), or
    :class:`~reyn.core.cancellable.Cancelled` if ``cancel_event`` fires first (#2813 —
    pass the per-turn ``OpContext.cancel_event`` here so a Ctrl-C during install
    interrupts the probe IMMEDIATELY instead of waiting out its own
    ``call_timeout_seconds``; ``None`` — the default — preserves the pre-#2813
    behavior of running to the probe's own timeout). ``Cancelled`` is deliberately NOT
    caught here — it propagates to the install caller, which translates it to a
    ``status:"cancelled"`` result (uniform with the ``mcp``/resource op cancel
    surface), rather than being flattened into a generic probe-error string. Either
    way the pool's ``__aexit__`` has already torn the transport down (stdio subprocess
    kill via ``kill_process_tree`` / HTTP close) by the time this function returns/
    raises, and because the config write is strictly AFTER this probe, nothing gets
    committed. **Transport-uniform**: stdio and remote (http/sse) share this one path —
    ``MCPClient.__aexit__`` owns the transport-appropriate teardown, so the probe never
    branches on transport (mirrors ``Session._mcp_list_tools``)."""
    from reyn.mcp.client import expand_env  # noqa: PLC0415
    from reyn.mcp.gateway import MCPFault, MCPGateway  # noqa: PLC0415

    expanded = expand_env(server_entry)
    if not isinstance(expanded, dict):
        return f"server config must be a dict, got {type(expanded).__name__}"
    # A url-only remote entry defaults to http (mirrors _mcp_list_tools).
    if "type" not in expanded and expanded.get("url"):
        expanded = {**expanded, "type": "http"}
    gateway = MCPGateway(agent_id=agent_id, cancel_event=cancel_event)
    try:
        await gateway.list_tools(server_name, expanded)
    except MCPFault as exc:
        return str(exc)
    return None


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


def _scan_install_metadata(
    command: str,
    args: list[str],
    desc: str,
    threat_scan: object | None,
) -> list:
    """Scan prospective MCP-install metadata for threats (#1863 / FP-0050 BP2).

    Pure: returns the de-duplicated list of ``ThreatMatch`` over the joined
    ``command + args + desc`` text, scanned under BOTH the ``exec`` and
    ``strict`` scopes. No events, no I/O — the caller emits telemetry and
    decides the block (via ``content_guard.first_blocking_match``).

    ``exec`` and ``strict`` are mutually non-subsuming scopes
    (``threat_patterns._SCOPE_INCLUDES``: exec=(all,exec),
    strict=(all,context,strict)); both are scanned and de-duplicated by
    ``pattern_id`` so the shared ``all`` patterns are not double-counted. The
    command/args carry exec-scope threats (pipe-to-shell / reverse-shell /
    download-then-exec); the description can hide strict-scope threats
    (ssh/secret access, config mutation in prose).

    Returns ``[]`` when ``threat_scan`` is absent/disabled or there is nothing
    to scan — making the whole feature a no-op unless the operator enabled it.
    """
    if threat_scan is None or not getattr(threat_scan, "enabled", False):
        return []
    scan_text = " ".join([command, *[str(a) for a in args], desc or ""]).strip()
    if not scan_text:
        return []
    from reyn.security.content_guard import scan_for_threats

    seen: set[str] = set()
    matches: list = []
    for scope in ("exec", "strict"):
        for m in scan_for_threats(scan_text, threat_scan, scope=scope):
            if m.pattern_id not in seen:
                seen.add(m.pattern_id)
                matches.append(m)
    return matches


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle(
    op: MCPInstallIROp,
    ctx: OpContext,
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
        # #1863: description for the install-time threat scan (SourceResolution
        # carries none today → empty; robust via getattr).
        install_desc = getattr(resolution, "description", "") or ""

    else:
        # Registry path (existing behaviour).
        from reyn.core.registry.client import RegistryClient, RegistryError

        try:
            async with RegistryClient(events=ctx.events) as client:
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
        # #1863: fetched server.json description for the install-time threat scan.
        install_desc = server_json.description or ""

    # ── 2. runtimeHint check ──────────────────────────────────────────────────
    if runtime and runtime in _RUNTIME_CMD:
        cmd = _RUNTIME_CMD[runtime]
        if shutil.which(cmd) is None:
            hint = _RUNTIME_INSTALL_HINT.get(runtime, f"'{cmd}' was not found")
            return {
                "kind": "mcp_install",
                "status": "error",
                "server_id": op.server_id,
                "error": f"Required runtime not found: '{cmd}'. {hint}",
            }

    # ── 2.5 Install-time threat scan (#1863 / FP-0050 BP2) ────────────────────
    # Scan the prospective launch command + args + fetched description BEFORE any
    # side effect (permission prompt, secret save, config write). A block-severity
    # hit denies the install via a structured ``status="blocked"`` result. The
    # scan logic lives in the pure ``_scan_install_metadata`` helper; here we wire
    # the command/args preview, emit telemetry, and short-circuit on a block.
    _ts = getattr(ctx, "threat_scan", None)
    _scan_command = ""
    _scan_args: list[str] = []
    if packages_raw:
        _preview_entry = _build_server_entry(packages_raw[0], [])
        _scan_command = str(_preview_entry.get("command", ""))
        _scan_args = [str(a) for a in (_preview_entry.get("args") or [])]
    _matches = _scan_install_metadata(_scan_command, _scan_args, install_desc, _ts)
    if _matches:
        from reyn.security.content_guard import first_blocking_match

        for _m in _matches:
            ctx.events.emit(
                "mcp_install_threat_match",
                pattern_id=_m.pattern_id,
                severity=_m.severity,
                scope=_m.scope,
            )
        _block = first_blocking_match(
            _matches, getattr(_ts, "block_severity", "block")
        )
        if _block is not None:
            ctx.events.emit(
                "mcp_install_threat_blocked",
                pattern_id=_block.pattern_id,
                severity=_block.severity,
                server_id=op.server_id,
            )
            return {
                "kind": "mcp_install",
                "status": "blocked",
                "server_id": op.server_id,
                "error": (
                    f"install blocked: fetched server metadata matched threat "
                    f"pattern '{_block.pattern_id}' "
                    f"({_block.scope}/{_block.severity}). The launch command or "
                    f"description contains a prohibited pattern (e.g. "
                    f"pipe-to-shell, reverse-shell, ssh/secret access, config "
                    f"mutation). Do not install this server."
                ),
            }

    # ── 3. Permission gate (#571 collapse arc Phase 5) ────────────────────────
    # The caller must declare ``file.write: [.reyn/mcp.yaml]`` (= the
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
        # #3089: thread ctx.intervention_bus through the write gate too — mirrors
        # the require_http_get call right below in this SAME function, which
        # already threads ctx.intervention_bus unconditionally (line ~435).
        # Without this, a narrowed sandbox_policy that puts config_path OUTSIDE
        # write_paths hard-denies with no prompt even when a real bus is sitting
        # on ctx (the CLI ``mcp install --source`` entry point already
        # constructs an OpContext with a real StdinInterventionBus —
        # interfaces/cli/commands/mcp.py — so a future sandbox-narrowing fix
        # there would silently regain the pre-#1505 hard-deny-only behavior
        # without this).
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=_sandbox, bus=ctx.intervention_bus,
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
                ctx.actor,
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
                ctx.permission_decl, key, ctx.actor,
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
                f"Could not auto-detect the runtime for GitHub URL '{op.source}'."
                " Specify an npm: / pypi: / docker: prefix explicitly, or use"
                " mcp__install_local to set command/args directly."
            ),
        }

    # Ensure the mcp section shape exists (read-only prep — NO write yet).
    if "mcp" not in existing or not isinstance(existing.get("mcp"), dict):
        existing["mcp"] = {}
    if "servers" not in existing["mcp"] or not isinstance(existing["mcp"].get("servers"), dict):
        existing["mcp"]["servers"] = {}

    # ── 5b. Probe-then-commit + path-condition (#2761 PR-3) ───────────────────
    # Capture pure-addition-vs-overwrite BEFORE any mutation. A PURE ADDITION on a
    # live per-session reloader takes the IMMEDIATE mid-turn path: PROBE the server
    # (spawn/connect + list_tools) FIRST, and write config ONLY on a successful probe
    # → a failed/cancelled probe leaves nothing committed (no half-install, no
    # rollback). A same-name overwrite (the documented `reyn mcp install` re-install
    # fix) or no per-session reloader keeps the existing deferred turn-boundary path
    # (unchanged), confining any in-use-replace to the deferred path.
    from reyn.core.cancellable import Cancelled  # noqa: PLC0415
    from reyn.runtime.hot_reload import (  # noqa: PLC0415
        dispatch_install_reload,
        is_pure_addition,
    )
    _is_addition = is_pure_addition(server_name, existing["mcp"]["servers"])
    _reloader = getattr(ctx, "hot_reloader", None)
    if _is_addition and _reloader is not None:
        try:
            _probe_err = await probe_mcp_server(
                server_name, server_entry, agent_id=getattr(ctx, "agent_id", None),
                cancel_event=ctx.cancel_event,
            )
        except Cancelled:
            # #2813: Ctrl-C during the probe → uniform status:"cancelled" (matches the
            # mcp/resource op cancel surface), nothing written (commit is strictly after).
            ctx.events.emit(
                "mcp_install_cancelled", server_id=op.server_id, server_name=server_name,
            )
            return {
                "kind": "mcp_install", "status": "cancelled",
                "server_id": op.server_id, "server_name": server_name,
                "source": op.source or "",
            }
        if _probe_err is not None:
            ctx.events.emit(
                "mcp_install_probe_failed",
                server_id=op.server_id,
                server_name=server_name,
                error=_probe_err,
            )
            return {
                "kind": "mcp_install",
                "status": "error",
                "server_id": op.server_id,
                "server_name": server_name,
                "source": op.source or "",
                "error": (
                    f"MCP server {server_name!r} failed its pre-install probe "
                    f"(nothing written — no half-install): {_probe_err}"
                ),
            }

    # ── 5c. COMMIT: write mcp.servers.<name>. The immediate path reaches here ONLY
    # after a successful probe; the deferred path always writes (unchanged). ──────
    existing["mcp"]["servers"][server_name] = server_entry

    _write_yaml_config(config_path, existing)

    # #2259 PR-1: record the FULL post-state as a truncation-surviving config generation so
    # the mcp registry recovers (the yaml is a derived projection). The helper guards
    # internally — no-op when there is no WAL or the path is outside the project `.reyn`.
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), config_path, existing)

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

    # ── 7. Reload (#2761 PR-3 + #2372): a PROBED pure addition on a live per-session
    # reloader applies IMMEDIATELY (mid-turn) — the mcp seam (_reapply_mcp →
    # refresh_mcp_servers) re-reads the roster from the config cascade (merging the
    # IN-set just written) and swaps the live tool-set, so the just-installed server's
    # tools are resolvable/callable this same turn. A same-name overwrite or no
    # per-session reloader (CLI `reyn mcp install` separate process) keeps the existing
    # deferred turn-boundary behavior via the process-active reloader (best-effort;
    # a restart / yaml mtime-watch still surfaces it). The LLM's tool *catalog* still
    # rebuilds next turn (discovery vs resolution) — the install op *uses* it this turn.
    await dispatch_install_reload(
        _reloader, source="mcp_install", is_addition=_is_addition,
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


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("mcp_install", handle, canonical=STRUCTURED_PASSTHROUGH)
