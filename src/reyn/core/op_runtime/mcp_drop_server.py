"""mcp_drop_server kind handler — remove an MCP server from configuration.

FP-0034 §D23: counter-op to mcp_install. Removes the server entry from
the scope-appropriate YAML config file and optionally cleans the
matching secret env keys from ~/.reyn/secrets.env.

Handler logic (one-shot, no sub-phases):
  1. Resolve scope — explicit (op.scope) or auto-detect by walking
     local → project → user tiers for the first match.
  2. Gate via PermissionResolver.require_mcp_drop_server (mirrors
     require_mcp_install but uses a distinct decl field).
  3. Capture the server's env block so we know which secret keys to
     clean up (= `${KEY}` references inside the entry).
  4. Remove the entry from yaml, prune empty `mcp.servers` / `mcp`
     containers so the file stays tidy. Write back.
  5. When op.clear_secrets=True, remove the captured keys from the
     user-level secrets store via reyn.security.secrets.store.clear_secret.
  6. Emit ``mcp_server_removed`` P6 event with the audit metadata.

Scope → file mapping mirrors mcp_install:
  local   → <project>/reyn.local.yaml
  project → <project>/reyn.yaml
  user    → ~/.reyn/config.yaml

This is a P5 exception: reyn.yaml lives outside the workspace, so the
OS handler writes it directly (same pattern as mcp_install). The
action is gated behind require_mcp_drop_server (FP-0034 §D23) and
recorded via event (P6), preserving the audit trail.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from reyn.schemas.models import MCPDropServerIROp

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx

# ---------------------------------------------------------------------------
# helpers (yaml read/write — mirror mcp_install for parity)
# ---------------------------------------------------------------------------


def _scope_to_path(scope: str, project_root: Path) -> Path:
    """Resolve the target config file path for the given scope.

    Issue #470 (2026-05-22): for the NEW dynamic registry location
    (``"dynamic"``), this returns ``.reyn/mcp.yaml``. Legacy scopes
    are retained so ``_detect_scope`` can walk older config files
    that still carry ``mcp.servers`` from before the separation.

    Migration order in ``_detect_scope`` queries ``"dynamic"`` first
    so new installs are dropped from the canonical location; older
    hand-edited entries in reyn.yaml / reyn.local.yaml / user config
    remain droppable via the legacy paths.
    """
    if scope == "dynamic":
        return project_root / ".reyn" / "mcp.yaml"
    if scope == "local":
        return project_root / "reyn.local.yaml"
    if scope == "project":
        return project_root / "reyn.yaml"
    return Path.home() / ".reyn" / "config.yaml"  # "user"


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
        yaml.dump(
            data, allow_unicode=True, default_flow_style=False, sort_keys=False,
        ),
        encoding="utf-8",
    )


def _detect_scope(server: str, project_root: Path) -> str | None:
    """Walk scope tiers to find the first that contains ``server``.

    Returns the scope name or None when the server is absent from all.
    Issue #470 (2026-05-22): ``"dynamic"`` (= ``.reyn/mcp.yaml``) is
    queried first because that's the canonical location for new
    installs. Older hand-edited entries in ``reyn.yaml`` /
    ``reyn.local.yaml`` / ``~/.reyn/config.yaml`` remain droppable
    via the legacy walk order — backward compat preserved.

    All scopes share the same ``{"mcp": {"servers": {...}}}`` shape
    so the lookup logic is uniform; only the file location differs.
    """
    for scope in ("dynamic", "local", "project", "user"):
        cfg = _read_yaml_config(_scope_to_path(scope, project_root))
        servers = (
            cfg.get("mcp", {}).get("servers", {})
            if isinstance(cfg.get("mcp"), dict)
            else {}
        )
        if isinstance(servers, dict) and server in servers:
            return scope
    return None


def _extract_env_keys(entry: dict) -> list[str]:
    """Return the secret env keys referenced by the server entry.

    The entry's ``env`` block maps env var names → ``${KEY}`` references
    (see mcp_install._build_server_entry). We collect the env var names
    themselves; the actual secret values live in ~/.reyn/secrets.env
    under those keys.
    """
    if not isinstance(entry, dict):
        return []
    env = entry.get("env")
    if not isinstance(env, dict):
        return []
    return [str(k) for k in env.keys()]


def _resolve_project_root(ctx: OpContext) -> Path:
    """Find the project root for scope file resolution.

    Reads from ``ctx.workspace.base_dir`` when available (= the
    canonical Workspace attribute set to CWD at construction time).
    Falls back to ``Path.cwd()`` for test contexts that pass a None
    workspace. Mirrors mcp_install's resolution semantics.
    """
    ws = getattr(ctx, "workspace", None)
    base = getattr(ws, "base_dir", None) if ws is not None else None
    if base is not None:
        return Path(str(base))
    return Path.cwd()


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(
    op: MCPDropServerIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute an mcp_drop_server op — remove a configured MCP server.

    Returns a dict with the canonical mcp_drop_server result shape:
      ``{kind, status, server, scope, removed_path, env_keys_cleared,
      secrets_cleared}``

    Raises:
        ValueError: when ``op.server`` is empty.
        PermissionError: when require_mcp_drop_server gate denies the op.
        FileNotFoundError-equivalent: when ``op.server`` is not present in
            any scope (returned as a ``{status: "not_found"}`` dict, not
            an exception — the LLM should be able to retry or move on).
    """
    server = (op.server or "").strip()
    if not server:
        raise ValueError("server must be a non-empty string")

    project_root = _resolve_project_root(ctx)

    # ── 1. Resolve scope ────────────────────────────────────────────────
    if op.scope is None:
        scope = _detect_scope(server, project_root)
        if scope is None:
            # Not found anywhere — return structured "not_found" rather
            # than raising. The LLM can list_actions to confirm what
            # servers exist and pick a different name.
            return {
                "kind": "mcp_drop_server",
                "status": "not_found",
                "server": server,
                "scope": None,
                "removed_path": None,
                "env_keys_cleared": [],
                "secrets_cleared": False,
            }
    else:
        scope = op.scope
        config_path = _scope_to_path(scope, project_root)
        existing = _read_yaml_config(config_path)
        servers = (
            existing.get("mcp", {}).get("servers", {})
            if isinstance(existing.get("mcp"), dict)
            else {}
        )
        if not (isinstance(servers, dict) and server in servers):
            return {
                "kind": "mcp_drop_server",
                "status": "not_found",
                "server": server,
                "scope": scope,
                "removed_path": str(config_path),
                "env_keys_cleared": [],
                "secrets_cleared": False,
            }

    config_path = _scope_to_path(scope, project_root)

    # ── 2. Permission gate (#571 collapse arc Phase 5) ─────────────────
    # The skill must declare ``file.write: [.reyn/mcp.yaml]``. The
    # bool-axis ``require_mcp_drop_server`` per-server prompt is
    # removed — per-server granularity in mutations is operator-
    # config-level concern, not per-op runtime concern.
    if ctx.permission_resolver is not None:
        # #1352-C: thread the agent/operator sandbox policy (SandboxLayer ∩),
        # same as op_runtime/file.py — was missing here (permission-only gap).
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.skill_name,
            sandbox_policy=_sandbox_policy_from_ctx(ctx),
        )

    # ── 3. Capture env keys before mutation ────────────────────────────
    existing = _read_yaml_config(config_path)
    mcp_dict = existing.get("mcp", {}) if isinstance(existing.get("mcp"), dict) else {}
    servers = mcp_dict.get("servers", {}) if isinstance(mcp_dict.get("servers"), dict) else {}
    entry = servers.get(server, {})
    env_keys = _extract_env_keys(entry)

    # ── 4. Remove the entry + prune empty containers ───────────────────
    if isinstance(existing.get("mcp"), dict):
        if isinstance(existing["mcp"].get("servers"), dict):
            existing["mcp"]["servers"].pop(server, None)
            if not existing["mcp"]["servers"]:
                del existing["mcp"]["servers"]
        if not existing["mcp"]:
            del existing["mcp"]
    _write_yaml_config(config_path, existing)

    # ── 5. Clean up secrets when requested ─────────────────────────────
    cleared_keys: list[str] = []
    if op.clear_secrets and env_keys:
        from reyn.security.secrets.store import clear_secret
        for key in env_keys:
            if clear_secret(key):
                cleared_keys.append(key)

    # ── 6. Emit mcp_server_removed event (P6) ──────────────────────────
    ctx.events.emit(
        "mcp_server_removed",
        server=server,
        scope=scope,
        removed_path=str(config_path),
        env_keys_captured=env_keys,
        secrets_cleared=cleared_keys,
        # NOTE: secret VALUES are NEVER emitted — only keys for audit.
    )

    return {
        "kind": "mcp_drop_server",
        "status": "ok",
        "server": server,
        "scope": scope,
        "removed_path": str(config_path),
        "env_keys_cleared": cleared_keys,
        "secrets_cleared": op.clear_secrets,
    }


register("mcp_drop_server", handle)
