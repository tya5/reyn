"""plugin_install kind handler — promote/install a self-contained plugin
directory (ADR 0064 §3.2/§3.8/§3.10/§3.11, P2 install machinery).

Reuses P1 (``reyn.plugins.{manifest,tokens,source}``) for the manifest
schema, ``${REYN_*}`` token expansion, and source-kind precedence, and
reuses skill_install.py's generic (skill-agnostic) helpers verbatim
(``_safe_skill_name`` / ``_contained_under`` / ``_source_host`` /
``_shallow_clone`` / ``_read_yaml`` / ``_write_yaml`` / ``_resolve_project_root``)
rather than re-implementing the sandboxed git-clone + path-traversal guards a
second time (mirrors how ``pipeline_install.py`` already does this).

Pipeline (one-shot, no sub-phases):

0. **Reconcile** any stale partial install left under ``~/.reyn/plugins/``
   from a previous crashed/interrupted install (§3.11) — self-healing on
   the next ``plugin_install`` call, since this repo has no general
   process-startup hook to run it at (documented scope choice, not a gap:
   the check is idempotent and cheap, so "next use" and "next start" both
   converge on the same safe state before a new install proceeds).
1. **Resolve source** → a source directory, per ``op.source.kind``:
   - ``builtin``: ``src/reyn/builtin/plugins/<name>/`` (reyn's own shipped).
   - ``local``: ``op.source.path`` directly (the author/test-loop's working
     copy — ADR §3.2's primary daily "promote" flow).
   - ``git``: gate ``require_http_get`` for the URL host, then shallow-clone
     to a staging dir under ``~/.reyn/plugins/.staging/`` (removed after the
     copy step, success or failure).
2. **Load + validate** ``.reyn-plugin/plugin.json`` via P1's
   ``load_plugin_manifest`` — a missing/malformed manifest refuses BEFORE
   any copy.
3. **Name-collision precedence** (§3.8/§3.10): when ``~/.reyn/plugins/<name>/``
   already holds a DIFFERENT-kind completed install, ``resolve_name_collision``
   decides the winner (builtin ≤ local ≪ git) — a lower-trust source is
   refused, never silently shadows a higher-trust one.
4. **Permission gate 1 — global-copy write**: ``require_file_write`` for
   ``~/.reyn/plugins/<name>/`` — this path is OUTSIDE the default write zone
   (``.reyn/`` under CWD), so the EXISTING gate mechanism already JIT-asks /
   denies for it (§3.10 item 1: composed from the existing gate, no new
   bool axis — the #571 collapse arc removed those).
5. **Copy**: write an ``.reyn-plugin/_install_state.json`` marker BEFORE
   copying content (so an interrupted copy is detectable — step 0's
   reconcile target), then copy the source tree (git clone's ``.git/``
   excluded) into the target dir.
6. **Expand ``${REYN_*}`` stable-location tokens** (P1 ``tokens.py``) —
   baked into the copied files, matching §3.4's "resolved once at copy
   time, inside the per-plugin copy dir" rule.
7. **Materialise deps** (§3.11): when the copied plugin carries a
   ``requirements.txt`` at its root, gate ``require_http_get`` for the
   package index host, then ``uv venv`` + ``uv pip install`` into
   ``<plugin_root>/.venv`` — network fetch happens HERE, at install time,
   never at spawn. When the mcp capability's ``.mcp.json`` declares
   ``command: "python"`` / ``"python3"``, the registered spawn command is
   rewritten to the materialised venv's interpreter — spawn is
   network-free by construction (the general form of #3060).
8. **Register**: for each capability the manifest declares, call the
   SAME existing register verbs — ``skill_install.handle`` /
   ``pipeline_install.handle`` for skills/pipelines (each op carries
   ``plugin_id=<name>``, §3.7's additive provenance field), and a direct
   ``.reyn/config/mcp.yaml`` write (mirrors ``mcp__install_local``'s shape,
   probe-then-commit) for the optional root ``.mcp.json``.
9. **Complete**: delete the ``_install_state.json`` marker (absence =
   completed — the state step 0's reconcile checks) and emit
   ``plugin_install_completed``.

Audit-events emitted (§3.11, at minimum): ``plugin_install_started`` /
``_copied`` / ``_deps_materialised`` / ``_registered`` / ``_completed``.

**Not WAL-derived** (§3.11): the ``~/.reyn/plugins/`` copies + the
materialised venv are FILES, not WAL-event-derived state — the
CLAUDE.md truncate-falsify recovery gate does not apply to them. The
reconcile in this module is a filesystem/registry consistency check;
the registry entries THEMSELVES (mcp/pipelines/skills.yaml) still ride
the existing config-generation recovery path via the sub-handlers they
call.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from reyn.plugins.manifest import (
    PluginManifestError,
    load_plugin_manifest,
    manifest_path_for,
)
from reyn.plugins.source import resolve_name_collision
from reyn.plugins.tokens import PluginTokenContext, expand_reyn_tokens
from reyn.schemas.models import PipelineInstallIROp, PluginInstallIROp, SkillInstallIROp

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx
from .pipeline_install import handle as _pipeline_install_handle

# Reuse skill_install's generic (plugin-agnostic) helpers verbatim — same
# rationale pipeline_install.py already documents for doing this.
from .skill_install import (
    _contained_under,
    _read_yaml,
    _resolve_project_root,
    _shallow_clone,
    _source_host,
    _write_yaml,
)
from .skill_install import (
    _safe_skill_name as _safe_name_component,
)
from .skill_install import handle as _skill_install_handle

_INSTALL_STATE_FILENAME = "_install_state.json"
_IGNORED_COPY_NAMES = {".git"}


# ---------------------------------------------------------------------------
# ~/.reyn/plugins/ layout helpers
# ---------------------------------------------------------------------------


def plugins_root() -> Path:
    """``~/.reyn/plugins/`` — the global plugin-code cache (ADR §3.3: code
    installs once to global, enablement is project-local)."""
    return Path.home() / ".reyn" / "plugins"


def _builtin_plugin_dir(name: str) -> Path:
    """``src/reyn/builtin/plugins/<name>/`` — reyn's own shipped plugins.

    Resolved package-relative (works identically in dev checkout and wheel
    install) rather than via ``resolve_reyn_root()`` — that function
    resolves reyn's REPO root (dev mode) vs installed-package dir (wheel
    mode), a distinction this lookup does not need: the ``builtin/``
    package ships inside ``reyn`` either way.
    """
    import reyn.builtin as _builtin_pkg
    return Path(_builtin_pkg.__file__).resolve().parent / "plugins" / name


def _install_state_path(plugin_root: Path) -> Path:
    return plugin_root / ".reyn-plugin" / _INSTALL_STATE_FILENAME


def _write_install_state(plugin_root: Path, kind: str) -> None:
    state_path = _install_state_path(plugin_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"name": plugin_root.name, "kind": kind, "status": "installing"}),
        encoding="utf-8",
    )


def _clear_install_state(plugin_root: Path) -> None:
    _install_state_path(plugin_root).unlink(missing_ok=True)


def _read_install_state(plugin_root: Path) -> dict | None:
    state_path = _install_state_path(plugin_root)
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reconcile_plugin_installs(root: "Path | None" = None) -> list[str]:
    """Filesystem-consistency reconcile (§3.11): any ``~/.reyn/plugins/<name>/``
    whose ``_install_state.json`` marker is STILL PRESENT never reached
    ``plugin_install_completed`` — a crash/interrupt mid-copy-or-later left a
    partial plugin that is neither usable nor cleanly removable via
    ``plugin_uninstall`` (its registry entries, if any, may be half-written).

    Chosen recovery: ROLL BACK (remove the partial ``<name>/`` directory)
    rather than "finish" — resuming a partial copy/materialise/register
    correctly requires knowing exactly which sub-step completed, which the
    marker does not (yet) distinguish; re-running the FULL install from
    scratch is cheap (the LLM just re-issues ``plugin_install``) and always
    safe, so it is the conservative default. Returns the list of plugin
    names rolled back.
    """
    base = root if root is not None else plugins_root()
    if not base.is_dir():
        return []
    rolled_back: list[str] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        state = _read_install_state(entry)
        if state is not None:
            shutil.rmtree(entry, ignore_errors=True)
            rolled_back.append(entry.name)
    # A staging clone dir interrupted mid-clone is never "installed" under
    # any name — always safe to sweep in full.
    staging = base / ".staging"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    return rolled_back


def _copy_plugin_tree(source_dir: Path, plugin_root: Path) -> None:
    """Copy ``source_dir``'s contents into ``plugin_root`` (which already
    exists — created by the caller so the ``_install_state.json`` marker can
    be written before any content lands), skipping VCS metadata."""
    for child in source_dir.iterdir():
        if child.name in _IGNORED_COPY_NAMES:
            continue
        dest = plugin_root / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)


def _expand_plugin_files(plugin_root: Path, token_ctx: PluginTokenContext) -> None:
    """Bake stable-location ``${REYN_*}`` tokens into every text file a
    capability might read (§3.4/§3.5): the root ``.mcp.json``, every
    ``pipelines/*.yaml``, and every ``skills/*/SKILL.md``. Non-existent
    globs are simply empty — every capability is optional (§3.1)."""
    candidates: list[Path] = [plugin_root / ".mcp.json"]
    pipelines_dir = plugin_root / "pipelines"
    if pipelines_dir.is_dir():
        candidates.extend(pipelines_dir.glob("*.yaml"))
    skills_dir = plugin_root / "skills"
    if skills_dir.is_dir():
        candidates.extend(skills_dir.glob("*/SKILL.md"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        expanded = expand_reyn_tokens(text, token_ctx)
        if expanded != text:
            path.write_text(expanded, encoding="utf-8")


async def _materialise_deps(
    plugin_root: Path, requirements: Path, ctx: OpContext,
) -> "tuple[Path | None, str | None]":
    """``uv venv`` + ``uv pip install -r requirements.txt`` into
    ``<plugin_root>/.venv`` (§3.11) — routed through the sandbox abstraction
    (mirrors ``skill_install._shallow_clone``'s rationale: an agent-reachable
    subprocess launch must never bypass ``reyn.security.sandbox``).

    Returns ``(venv_python, None)`` on success (the venv's interpreter path,
    for the mcp-registration step to point spawn at), or ``(None, error)`` on
    failure. Network is scoped to THIS install-time step only; the venv
    itself carries no network policy of its own — that governs SPAWN, which
    this step never touches.
    """
    from reyn.security.sandbox import SandboxPolicy, get_default_backend

    backend = ctx.sandbox_backend or get_default_backend(ctx.sandbox_config)
    venv_dir = plugin_root / ".venv"
    policy = SandboxPolicy(
        network=True,
        write_paths=[str(plugin_root)],
        timeout_seconds=300,
        allow_subprocess=True,
        env_passthrough=[
            "HOME", "PATH",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "no_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR",
            "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "PIP_INDEX_URL",
        ],
    )
    try:
        venv_result = await backend.run(
            ["uv", "venv", str(venv_dir)], policy, cwd=str(plugin_root),
        )
    except Exception as exc:  # noqa: BLE001 — surface as a materialise error, not a crash
        return None, f"uv venv error: {exc}"
    if venv_result.returncode != 0:
        detail = venv_result.stderr.decode("utf-8", errors="replace").strip()
        return None, f"uv venv failed (exit {venv_result.returncode}): {detail}"

    venv_python = venv_dir / "bin" / "python"
    try:
        install_result = await backend.run(
            ["uv", "pip", "install", "--python", str(venv_python), "-r", str(requirements)],
            policy,
            cwd=str(plugin_root),
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"uv pip install error: {exc}"
    if install_result.returncode != 0:
        detail = install_result.stderr.decode("utf-8", errors="replace").strip()
        return None, f"uv pip install failed (exit {install_result.returncode}): {detail}"
    return venv_python, None


def _mcp_config_path(project_root: Path) -> Path:
    return project_root / ".reyn" / "config" / "mcp.yaml"


def _build_mcp_entries(mcp_json: Path, venv_python: "Path | None") -> dict:
    """Parse the plugin's root ``.mcp.json`` (standard shape,
    ``{"mcpServers": {"<name>": {"command", "args", "env"?, "url"?}}}``)
    into reyn's ``mcp.servers.<name>`` entry shape.

    When ``venv_python`` is set and a server's ``command`` is exactly
    ``"python"``/``"python3"``, the command is rewritten to the
    materialised venv's interpreter — the spawn-is-network-free swap
    (§3.11's "the registered spawn command points at that ready env's
    interpreter")."""
    try:
        raw = json.loads(mcp_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        if "url" in spec:
            entry: dict = {"type": spec.get("type", "http"), "url": spec["url"]}
        else:
            command = str(spec.get("command", ""))
            if venv_python is not None and command in ("python", "python3"):
                command = str(venv_python)
            entry = {
                "type": "stdio",
                "command": command,
                "args": [str(a) for a in spec.get("args", [])],
            }
        env = spec.get("env")
        if isinstance(env, dict):
            entry["env"] = {str(k): str(v) for k, v in env.items()}
        out[str(name)] = entry
    return out


async def _register_mcp(
    plugin_root: Path, plugin_name: str, venv_python: "Path | None",
    ctx: OpContext, project_root: Path,
) -> list[str]:
    """Register every server declared in the plugin's root ``.mcp.json``
    into ``.reyn/config/mcp.yaml`` — mirrors ``mcp__install_local``'s shape
    (probe-then-commit on a live per-session reloader; deferred write
    otherwise), tagged with ``plugin_id`` (§3.7) so ``plugin_uninstall`` can
    find these entries again."""
    mcp_json = plugin_root / ".mcp.json"
    entries = _build_mcp_entries(mcp_json, venv_python)
    if not entries:
        return []

    from reyn.core.events.config_recovery import record_config_generation
    from reyn.core.op_runtime.mcp_install import probe_mcp_server
    from reyn.runtime.hot_reload import dispatch_install_reload, is_pure_addition

    config_path = _mcp_config_path(project_root)
    data = _read_yaml(config_path)
    servers = data.setdefault("mcp", {}).setdefault("servers", {})

    registered: list[str] = []
    for name, entry in entries.items():
        is_addition = is_pure_addition(name, servers)
        reloader = getattr(ctx, "hot_reloader", None)
        if is_addition and reloader is not None:
            probe_err = await probe_mcp_server(
                name, entry, agent_id=getattr(ctx, "agent_id", None),
                cancel_event=getattr(ctx, "cancel_event", None),
            )
            if probe_err is not None:
                # Probe-then-commit: skip this one server (nothing written
                # for it) rather than fail the whole plugin install — other
                # capabilities may still be perfectly usable.
                continue
        entry["plugin_id"] = plugin_name
        servers[name] = entry
        registered.append(name)

    if registered:
        _write_yaml(config_path, data)
        await record_config_generation(getattr(ctx, "state_log", None), config_path, data)
        for name in registered:
            ctx.events.emit(
                "mcp_server_installed", server_id=name, server_name=name,
                scope="local", runtime="stdio", installed_path=str(config_path),
                source=f"plugin_install:{plugin_name}",
            )
        await dispatch_install_reload(
            getattr(ctx, "hot_reloader", None), source="mcp__install_local",
            is_addition=True,
        )
    return registered


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(op: PluginInstallIROp, ctx: OpContext) -> dict:
    project_root = _resolve_project_root(ctx.workspace)
    root = plugins_root()

    # ── 0. Reconcile stale partial installs (§3.11) ───────────────────────────
    reconcile_plugin_installs(root)

    staging_cleanup: "Path | None" = None
    source_kind = op.source.kind

    # ── 1. Resolve source directory ────────────────────────────────────────────
    if source_kind == "builtin":
        source_dir = _builtin_plugin_dir(op.source.name)
        if not source_dir.is_dir():
            return {
                "kind": "plugin_install", "status": "error",
                "error": f"unknown builtin plugin {op.source.name!r} (no "
                         f"src/reyn/builtin/plugins/{op.source.name}/ directory).",
            }
    elif source_kind == "local":
        source_dir = Path(op.source.path)
        if not source_dir.is_dir():
            return {
                "kind": "plugin_install", "status": "error",
                "error": f"local plugin path {op.source.path!r} is not a directory.",
            }
    else:  # git
        host = _source_host(op.source.url)
        if ctx.permission_resolver is not None and host is not None:
            sandbox = _sandbox_policy_from_ctx(ctx)
            await ctx.permission_resolver.require_http_get(
                ctx.permission_decl, host, ctx.intervention_bus, ctx.actor,
                sandbox_policy=sandbox,
            )
        staging = root / ".staging" / f"git-{uuid4().hex}"
        clone_err = await _shallow_clone(op.source.url, staging, ctx)
        if clone_err:
            return {
                "kind": "plugin_install", "status": "error",
                "error": clone_err,
            }
        source_dir = staging
        staging_cleanup = staging

    # ── 2. Load + validate the manifest ───────────────────────────────────────
    try:
        manifest = load_plugin_manifest(source_dir)
    except PluginManifestError as exc:
        if staging_cleanup:
            shutil.rmtree(staging_cleanup, ignore_errors=True)
        return {"kind": "plugin_install", "status": "error", "error": str(exc)}

    raw_name = (op.name or manifest.name or "").strip()
    safe_name = _safe_name_component(raw_name)
    if safe_name is None:
        if staging_cleanup:
            shutil.rmtree(staging_cleanup, ignore_errors=True)
        return {
            "kind": "plugin_install", "status": "error",
            "error": f"invalid plugin name {raw_name!r}: must be a single safe "
                     "path component (letters, digits, '.', '_', '-'; no '/', "
                     "'\\', '..', or leading '.').",
        }

    plugin_root = root / safe_name

    # SECURITY: belt-and-suspenders containment — refuse if plugin_root escapes
    # ~/.reyn/plugins/ even after sanitization (guards a sanitizer gap). No
    # filesystem mutation happens before this check passes (mirrors
    # skill_install's / pipeline_install's identical guard).
    if not _contained_under(plugin_root, root):
        if staging_cleanup:
            shutil.rmtree(staging_cleanup, ignore_errors=True)
        return {
            "kind": "plugin_install", "status": "error", "name": safe_name,
            "error": f"refused: install destination for {safe_name!r} escapes "
                     "~/.reyn/plugins/. This is a path-containment violation.",
        }

    # ── 3. Name-collision precedence (§3.8/§3.10) ─────────────────────────────
    existing_state = _read_install_state(plugin_root)
    existing_kind = None
    if plugin_root.is_dir() and existing_state is None:
        # A completed prior install has no _install_state.json marker (cleared
        # on success) — its own kind is recorded in .reyn-plugin/plugin.json's
        # sibling manifest read is not authoritative for SOURCE kind, so a
        # completed install's provenance is tracked via a lightweight sidecar
        # written alongside the manifest at registration time (below).
        existing_kind = _read_completed_kind(plugin_root)
    if existing_kind is not None and existing_kind != source_kind:
        winner = resolve_name_collision([existing_kind, source_kind])
        if winner != source_kind:
            if staging_cleanup:
                shutil.rmtree(staging_cleanup, ignore_errors=True)
            return {
                "kind": "plugin_install", "status": "skipped", "name": safe_name,
                "error": f"plugin {safe_name!r} is already installed from a "
                         f"higher-trust {existing_kind!r} source; refusing to "
                         f"shadow it with a {source_kind!r} source (ADR 0064 "
                         "§3.8 precedence: builtin <= local << git).",
            }

    ctx.events.emit("plugin_install_started", name=safe_name, source_kind=source_kind)

    # ── 4. Permission gate 1 — global-copy write outside the workspace ────────
    if ctx.permission_resolver is not None:
        sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(plugin_root), ctx.actor,
            sandbox_policy=sandbox, bus=ctx.intervention_bus,
        )

    # ── 5. Copy ─────────────────────────────────────────────────────────────
    plugin_root.mkdir(parents=True, exist_ok=True)
    _write_install_state(plugin_root, source_kind)
    _copy_plugin_tree(source_dir, plugin_root)
    if staging_cleanup:
        shutil.rmtree(staging_cleanup, ignore_errors=True)
    ctx.events.emit("plugin_install_copied", name=safe_name, plugin_root=str(plugin_root))

    # ── 6. Expand ${REYN_*} stable-location tokens ────────────────────────────
    token_ctx = PluginTokenContext(plugin_root=plugin_root, project_dir=project_root)
    _expand_plugin_files(plugin_root, token_ctx)

    # ── 7. Materialise deps (§3.11 — install-time network, network-free spawn) ─
    venv_python: "Path | None" = None
    requirements = plugin_root / "requirements.txt"
    if requirements.is_file():
        if ctx.permission_resolver is not None:
            sandbox = _sandbox_policy_from_ctx(ctx)
            await ctx.permission_resolver.require_http_get(
                ctx.permission_decl, "pypi.org", ctx.intervention_bus, ctx.actor,
                sandbox_policy=sandbox,
            )
        venv_python, materialise_err = await _materialise_deps(plugin_root, requirements, ctx)
        if materialise_err:
            # Leave the _install_state.json marker in place — the next
            # plugin_install call's reconcile pass (step 0) rolls this
            # partial install back.
            return {
                "kind": "plugin_install", "status": "error", "name": safe_name,
                "error": f"dependency materialisation failed: {materialise_err}",
            }
    ctx.events.emit(
        "plugin_install_deps_materialised", name=safe_name,
        materialised=venv_python is not None,
    )

    # ── 8. Register capabilities ──────────────────────────────────────────────
    manifest_path = manifest_path_for(plugin_root)
    reloaded_manifest = load_plugin_manifest(plugin_root) if manifest_path.exists() else manifest
    registered: dict[str, list] = {"mcp": [], "pipelines": [], "skills": []}

    for cap in reloaded_manifest.capabilities:
        if cap.kind == "mcp":
            registered["mcp"] = await _register_mcp(
                plugin_root, safe_name, venv_python, ctx, project_root,
            )
        elif cap.kind == "pipelines":
            pipelines_dir = plugin_root / "pipelines"
            files = (
                [pipelines_dir / e for e in cap.entries]
                if cap.entries
                else (sorted(pipelines_dir.glob("*.yaml")) if pipelines_dir.is_dir() else [])
            )
            for dsl_file in files:
                sub_op = PipelineInstallIROp(
                    kind="pipeline_install", path=str(dsl_file), plugin_id=safe_name,
                )
                sub_result = await _pipeline_install_handle(sub_op, ctx)
                registered["pipelines"].append(sub_result)
        elif cap.kind == "skills":
            skills_dir = plugin_root / "skills"
            dirs = (
                [skills_dir / e for e in cap.entries]
                if cap.entries
                else (sorted(p for p in skills_dir.glob("*") if p.is_dir()) if skills_dir.is_dir() else [])
            )
            for skill_dir in dirs:
                sub_op = SkillInstallIROp(
                    kind="skill_install", path=str(skill_dir), plugin_id=safe_name,
                )
                sub_result = await _skill_install_handle(sub_op, ctx)
                registered["skills"].append(sub_result)

    ctx.events.emit("plugin_install_registered", name=safe_name, registered=registered)

    # ── 9. Complete ────────────────────────────────────────────────────────────
    _clear_install_state(plugin_root)
    _write_completed_kind(plugin_root, source_kind)
    ctx.events.emit("plugin_install_completed", name=safe_name)

    return {
        "status": "installed",
        "name": safe_name,
        "plugin_root": str(plugin_root),
        "source_kind": source_kind,
        "capabilities": sorted(reloaded_manifest.capability_kinds),
        "registered": registered,
    }


# ---------------------------------------------------------------------------
# Completed-install provenance sidecar (name-collision precedence, §3.8)
# ---------------------------------------------------------------------------
# Separate from _install_state.json (which tracks in-progress vs completed):
# this tiny sidecar survives the whole plugin lifetime so a LATER install
# call for the same name can read back WHICH kind is currently installed,
# without re-deriving it from ambiguous evidence (the manifest itself
# carries no source-kind field — a plugin doesn't know how it was fetched).

_PROVENANCE_FILENAME = "_source_kind.json"


def _provenance_path(plugin_root: Path) -> Path:
    return plugin_root / ".reyn-plugin" / _PROVENANCE_FILENAME


def _write_completed_kind(plugin_root: Path, kind: str) -> None:
    path = _provenance_path(plugin_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": kind}), encoding="utf-8")


def _read_completed_kind(plugin_root: Path) -> "str | None":
    path = _provenance_path(plugin_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    kind = data.get("kind") if isinstance(data, dict) else None
    return kind if isinstance(kind, str) else None


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("plugin_install", handle, canonical=STRUCTURED_PASSTHROUGH)
