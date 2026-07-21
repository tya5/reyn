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
   ``requirements.txt`` at its root, the pypi.org dep-fetch approval is
   **derived from the install's own gate-1 write-approval** (#3048 —
   ``session_approve_host("pypi.org", ...)`` scoped to exactly that host,
   never a blanket http.get grant, so a separate interactive prompt is
   never raised for it), then ``require_http_get`` is still called (config
   deny / sandbox network-veto keep applying on top of the derive), then
   ``<sys.executable> -m venv`` + ``<venv_python> -m pip install`` into
   ``<plugin_root>/.venv`` (#3202 symptom 2 — no ``uv`` dependency; reyn's
   own interpreter + the stdlib ``venv``/``pip`` it ships with) — network
   fetch happens HERE, at install time, never at spawn. Without the
   derive, a codeact/headless dispatch (a bus wired but nothing answers
   it) awaits that prompt indefinitely and gets guillotined by the
   caller's compute-budget timeout. When the mcp capability's
   ``.mcp.json`` declares ``command: "python"`` / ``"python3"``, the
   registered spawn command is rewritten to the materialised venv's
   interpreter — spawn is network-free by construction (the general form
   of #3060).
8. **Register**: for each capability the manifest declares, call the
   SAME existing register verbs — ``skill_install.handle`` /
   ``pipeline_install.handle`` for skills/pipelines (each op carries
   ``plugin_id=<name>``, §3.7's additive provenance field), and a
   ``require_file_write``-gated (#3088) direct ``.reyn/config/mcp.yaml``
   write (mirrors ``mcp__install_local``'s shape, probe-then-commit) for
   the optional root ``.mcp.json``.
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
import os
import shutil
import sys
import sysconfig
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


# ---------------------------------------------------------------------------
# Registry-drop helpers (shared by plugin_uninstall + reconcile, §3.7/§3.11)
# ---------------------------------------------------------------------------
# A plugin's registered capabilities live in the SAME three project registries
# skill_install / pipeline_install / a local mcp entry write, each entry tagged
# with ``plugin_id`` (§3.7). Uninstall AND reconcile-rollback both need to drop
# every entry a given plugin_id created — so the pure "find + remove by
# plugin_id" logic lives here once and both callers reuse it (uninstall wraps
# it with the operator permission gate; reconcile calls it ungated as OS-
# internal consistency repair — see reconcile_plugin_installs).

_REGISTRY_KINDS: tuple[str, ...] = ("mcp", "pipelines", "skills")


def registry_config_paths(project_root: Path) -> "dict[str, Path]":
    """The three per-project capability-registry config files."""
    config_dir = project_root / ".reyn" / "config"
    return {
        "mcp": config_dir / "mcp.yaml",
        "pipelines": config_dir / "pipelines.yaml",
        "skills": config_dir / "skills.yaml",
    }


def _registry_entries_key(registry_kind: str) -> str:
    """mcp nests under ``servers``; pipelines/skills under ``entries``."""
    return "servers" if registry_kind == "mcp" else "entries"


def registry_entries_section(data: dict, registry_kind: str) -> "dict | None":
    """Return the ``<registry_kind>.<entries|servers>`` mapping, or None when
    absent/malformed."""
    section = data.get(registry_kind)
    if not isinstance(section, dict):
        return None
    entries = section.get(_registry_entries_key(registry_kind))
    return entries if isinstance(entries, dict) else None


def drop_entries_by_plugin_id(
    data: dict, registry_kind: str, plugin_name: str,
) -> list[str]:
    """PURE: remove every entry in ``data``'s ``<registry_kind>`` section tagged
    ``plugin_id == plugin_name``, mutating ``data`` in place. Returns the
    removed entry names (empty when the section is absent or nothing matched)."""
    entries = registry_entries_section(data, registry_kind)
    if not entries:
        return []
    to_remove = [
        name for name, entry in entries.items()
        if isinstance(entry, dict) and entry.get("plugin_id") == plugin_name
    ]
    for name in to_remove:
        del entries[name]
    return to_remove


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


async def reconcile_plugin_installs(
    root: "Path | None" = None,
    *,
    project_root: "Path | None" = None,
    state_log: "object | None" = None,
    events: "object | None" = None,
) -> list[str]:
    """Filesystem+registry-consistency reconcile (§3.11): any
    ``~/.reyn/plugins/<name>/`` whose ``_install_state.json`` marker is STILL
    PRESENT never reached ``plugin_install_completed`` — a crash/interrupt
    mid-copy-or-later left a partial plugin that is neither usable nor cleanly
    removable via ``plugin_uninstall`` (its registry entries, if any, may be
    half-written).

    Chosen recovery: ROLL BACK rather than "finish" — resuming a partial
    copy/materialise/register correctly requires knowing exactly which sub-step
    completed, which the marker does not (yet) distinguish; re-running the FULL
    install from scratch is cheap (the LLM just re-issues ``plugin_install``)
    and always safe, so it is the conservative default.

    **Rollback mirrors uninstall's drop-registry-FIRST ordering (§3.11).** A
    partial install may have crashed AFTER registering some capabilities but
    before completing — leaving registry entries tagged with the partial's
    ``plugin_id`` that point at a directory this reconcile is about to delete.
    Dropping the copy WITHOUT dropping those entries would leave a **dangling
    registry entry** (a skill/pipeline/mcp entry whose ``path`` no longer
    exists). So when ``project_root`` is supplied, each rolled-back plugin's
    entries are dropped from all three ``.reyn/config/*.yaml`` registries
    BEFORE its copy is removed. The registry-drop is UNGATED here (unlike
    ``plugin_uninstall``, which is an operator-initiated action): reconcile is
    OS-internal consistency repair removing entries that are already broken
    (they reference a directory being deleted), so it needs no operator
    consent — removing a dangling entry is always the safe/correct repair. Each
    dropped registry still records a config generation (recovery-core) so the
    repair survives rewind/crash the same way the install did.

    ``project_root`` omitted (a bare filesystem sweep, e.g. the standalone
    test/CLI path) drops no registry entries — only the copies — which is the
    correct behavior when there is no project registry in scope.

    Returns the list of plugin names rolled back.
    """
    base = root if root is not None else plugins_root()
    if not base.is_dir():
        return []
    rolled_back: list[str] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        state = _read_install_state(entry)
        if state is None:
            continue
        # Drop-registry-FIRST (§3.11): remove any entries this partial install
        # registered before deleting the copy they point at.
        if project_root is not None:
            await _reconcile_drop_registry_entries(
                project_root, entry.name, state_log=state_log, events=events,
            )
        shutil.rmtree(entry, ignore_errors=True)
        rolled_back.append(entry.name)
        if events is not None:
            events.emit("plugin_install_reconciled", name=entry.name, action="rolled_back")
    # A staging clone dir interrupted mid-clone is never "installed" under
    # any name — always safe to sweep in full.
    staging = base / ".staging"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    return rolled_back


async def _reconcile_drop_registry_entries(
    project_root: Path, plugin_name: str,
    *, state_log: "object | None", events: "object | None",
) -> dict[str, list[str]]:
    """Drop every ``.reyn/config/{mcp,pipelines,skills}.yaml`` entry tagged
    ``plugin_id == plugin_name`` (UNGATED — OS-internal repair; see
    ``reconcile_plugin_installs``). Records a config generation per touched
    file so the repair is recovery-visible."""
    from reyn.core.events.config_recovery import record_config_generation

    removed: dict[str, list[str]] = {}
    for registry_kind, config_path in registry_config_paths(project_root).items():
        if not config_path.exists():
            removed[registry_kind] = []
            continue
        data = _read_yaml(config_path)
        dropped = drop_entries_by_plugin_id(data, registry_kind, plugin_name)
        removed[registry_kind] = dropped
        if dropped:
            _write_yaml(config_path, data)
            await record_config_generation(state_log, config_path, data)
    return removed


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
    mcp_and_pipeline_candidates: list[Path] = [plugin_root / ".mcp.json"]
    pipelines_dir = plugin_root / "pipelines"
    if pipelines_dir.is_dir():
        mcp_and_pipeline_candidates.extend(pipelines_dir.glob("*.yaml"))
    for path in mcp_and_pipeline_candidates:
        _bake_all_tokens(path, token_ctx)

    # SKILL.md bakes ONLY ${REYN_PLUGIN_ROOT} here — ${REYN_PROJECT_DIR} is a
    # dynamic param (§3.4), never baked at copy: the plugin's global
    # ~/.reyn/plugins/ copy can be ENABLED into many different projects
    # (§3.3 — code installs once globally, enablement is project-local), so
    # baking THIS install call's project_root into the shared copy would
    # freeze every future enabling project to whichever one happened to
    # install it first. ${REYN_SKILL_DIR} is left unbaked too. Both resolve
    # fresh at invocation instead, via the skill-load verb
    # (`reyn.plugins.skill_load.load_skill_body`, P4/#3070).
    skills_dir = plugin_root / "skills"
    if skills_dir.is_dir():
        for path in skills_dir.glob("*/SKILL.md"):
            _bake_plugin_root_only(path, token_ctx.plugin_root)


def _bake_all_tokens(path: Path, token_ctx: PluginTokenContext) -> None:
    """Expand every ``${REYN_*}`` token *token_ctx* carries a value for, in
    place — the mcp/pipeline copy-time bake, unchanged from pre-#3070
    behavior."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    expanded = expand_reyn_tokens(text, token_ctx)
    if expanded != text:
        path.write_text(expanded, encoding="utf-8")


def _bake_plugin_root_only(path: Path, plugin_root: Path) -> None:
    """Expand ONLY ``${REYN_PLUGIN_ROOT}`` in *path*, in place — every other
    ``${REYN_*}``/``${CLAUDE_*}``/``${env:...}`` token is left as a literal
    string for the invocation-time skill-load pass. A targeted string
    replace rather than ``expand_reyn_tokens`` (whose ``PluginTokenContext``
    requires ``project_dir``, which this call must NOT supply a baked value
    for — see the caller's docstring)."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    expanded = text.replace("${REYN_PLUGIN_ROOT}", str(plugin_root))
    if expanded != text:
        path.write_text(expanded, encoding="utf-8")


def _venv_interpreter_path_discover(venv_dir: Path) -> Path:
    """FALLBACK ONLY (secondary) — see ``_venv_interpreter_path`` for the
    primary mechanism. Discover ``venv_dir``'s Python interpreter by
    checking which of the two PEP 405 venv layouts actually exists on disk
    — POSIX's ``<venv>/bin/python`` or Windows's (incl. git-bash, which
    still runs a Windows-layout venv) ``<venv>/Scripts/python.exe`` — rather
    than *constructing* the path from ``os.name``/``sys.platform``. This is
    only reached if the primary ``sysconfig``-based computation somehow
    fails (should not happen in practice — this is the pathological-failure
    safety net, not the normal path).

    Raises ``FileNotFoundError`` if NEITHER layout exists — that is not a
    layout question but evidence venv creation did not actually materialise
    an interpreter (should have already been caught by its exit code; this
    is the decision-enabling explicit failure, not a silent None).
    """
    windows_python = venv_dir / "Scripts" / "python.exe"
    posix_python = venv_dir / "bin" / "python"
    if windows_python.exists():
        return windows_python
    if posix_python.exists():
        return posix_python
    raise FileNotFoundError(
        f"no venv interpreter found under {venv_dir!s} "
        f"(checked {windows_python!s} and {posix_python!s}) — "
        "venv creation may not have actually materialised an interpreter"
    )


def _venv_interpreter_path(venv_dir: Path) -> Path:
    """Resolve ``venv_dir``'s Python interpreter path using the stdlib
    ``sysconfig`` module's ``"venv"`` install scheme — no hardcoded
    ``bin``/``Scripts`` branch, no subprocess, no third-party tool (uv or
    otherwise). #3202 symptom 1 was a hardcoded ``<venv>/bin/python``
    construction: a real path on Linux/macOS CI, absent on Windows (there
    it's ``<venv>/Scripts/python.exe``), so ``uv pip install --python <that
    path>`` and MCP spawn both failed there even though venv creation
    itself succeeded.

    Ground (checked directly on this machine's CPython, not assumed):
    ``sysconfig.get_paths(scheme="venv", vars={"base": <venv_dir>,
    "platbase": <venv_dir>})["scripts"]`` returns the venv's script/bin
    directory for THE CURRENT INTERPRETER'S OS — the ``"venv"`` scheme name
    is itself resolved by ``sysconfig`` internally to the OS-appropriate
    scheme (``posix_venv`` gives ``{base}/bin``; ``nt_venv`` gives
    ``{base}/Scripts``) at *sysconfig import time*, i.e. it already encodes
    the platform branch so this call site never has to. The interpreter
    filename is ``"python" + sysconfig.get_config_var("EXE")`` (``EXE`` is
    ``""`` on POSIX, ``".exe"`` on Windows) — verified against a real
    ``python -m venv``-created venv: the computed path exists and matches
    the actual interpreter file on disk.

    ``_materialise_deps`` creates the venv with ``<reyn's own python> -m
    venv`` (the stdlib ``venv`` module, not a third-party tool with its own
    possibly-different layout), so this computation targets exactly the
    layout that module produces — not a guess about what SOME tool might
    have done.

    Falls back to ``_venv_interpreter_path_discover``'s on-disk existence
    check only if this computation's result does not actually exist (a
    pathological case — e.g. a ``sysconfig`` customisation on some
    distro) — a decision-enabling last resort, not the main path.

    This is the ONE place that resolves the interpreter path — both the MCP
    spawn command rewrite (which must name a real executable to exec) and
    the install step's ``pip install`` invocation (this module) key off it.
    Keeping it singular means the next call site never re-invents the POSIX
    assumption.
    """
    scripts_dir = Path(
        sysconfig.get_paths(
            scheme="venv", vars={"base": str(venv_dir), "platbase": str(venv_dir)},
        )["scripts"],
    )
    exe_suffix = sysconfig.get_config_var("EXE") or ""
    computed = scripts_dir / f"python{exe_suffix}"
    if computed.exists():
        return computed
    # sysconfig computed a path that isn't actually there — pathological;
    # fall back to on-disk discovery before giving up.
    return _venv_interpreter_path_discover(venv_dir)


async def _materialise_deps(
    plugin_root: Path, requirements: Path, ctx: OpContext,
) -> "tuple[Path | None, str | None]":
    """``<sys.executable> -m venv`` + ``<venv_python> -m pip install -r
    requirements.txt`` into ``<plugin_root>/.venv`` (§3.11) — routed through
    the sandbox abstraction (mirrors ``skill_install._shallow_clone``'s
    rationale: an agent-reachable subprocess launch must never bypass
    ``reyn.security.sandbox``).

    Uses the stdlib ``venv`` module (via reyn's OWN interpreter,
    ``sys.executable`` — guaranteed present, no external tool dependency)
    and ``pip`` (bundled with every CPython venv) rather than ``uv`` —
    #3202 symptom 2. ``uv`` was never load-bearing here: no lockfile is
    used (plain ``requirements.txt``), reyn's own runtime already requires
    a working CPython, and an operator missing ``uv`` on ``PATH`` (the
    reported failure mode — Windows/git-bash environments where ``uv`` is
    an extra, easy-to-miss install) got a confusing "run 'uv venv'" error
    for a tool reyn itself never asked them to install. Ground before this
    change: `<probe>/.venv/bin/python -m pip install sqlite-vec>=0.1.9
    apsw>=3.51 chonkie>=1.7 fastmcp>=2.0` (the plugin's actual
    ``requirements.txt``) resolves and installs cleanly with plain pip,
    including ``sqlite-vec`` (wheel-only, no sdist — the one dependency
    most likely to need a resolver's special handling; it did not).

    Returns ``(venv_python, None)`` on success (the venv's interpreter path,
    for the mcp-registration step to point spawn at), or ``(None, error)`` on
    failure. Network is scoped to THIS install-time step only; the venv
    itself carries no network policy of its own — that governs SPAWN, which
    this step never touches (unchanged from the ``uv``-based version:
    spawn execs the frozen absolute interpreter path directly, #3060).
    """
    from reyn.security.sandbox import SandboxPolicy, get_default_backend

    backend = ctx.sandbox_backend or get_default_backend(ctx.sandbox_config)
    venv_dir = plugin_root / ".venv"
    # pip's default cache dir is OUTSIDE the sandbox write_paths (scoped
    # tight to plugin_root), so under an enforcing backend (Seatbelt/
    # Landlock) pip's default cache write is denied and materialise fails
    # with "Operation not permitted" — same reasoning that applied to uv's
    # cache-dir before this change. Point the cache INSIDE plugin_root (within
    # write_paths) via ``--cache-dir`` — a CLI arg, so no os.environ mutation
    # and no broad ambient-cache grant.
    cache_dir = plugin_root / ".pip-cache"
    # pip additionally unpacks wheels into `tempfile.mkdtemp()` (Python's
    # tempfile module, which consults `TMPDIR`/`TEMP`/`TMP` before falling
    # back to system `/tmp`) during install — a WRITE that, unlike the cache
    # dir above, is not steerable via a pip CLI flag. Left at its default,
    # that write lands OUTSIDE `write_paths` (system `/tmp`, not
    # plugin_root), so an enforcing backend denies it exactly like the
    # cache-dir issue this comment already documents. Give it its own
    # dedicated dir INSIDE plugin_root and point `TMPDIR` there (narrowly
    # scoped, not a broad ambient-`/tmp` grant).
    tmp_dir = plugin_root / ".pip-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
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
            "PIP_INDEX_URL",
            "TMPDIR",
        ],
    )
    # `env_passthrough` only copies EXISTING os.environ values through to the
    # sandboxed child (see `resolve_passthrough_env`) — there is no
    # backend.run() parameter to inject a NEW env value, so scoping TMPDIR to
    # this install requires briefly setting it in THIS process's environ,
    # restored in `finally`. This mutates process-global state for the
    # duration of the two subprocess calls below; concurrent
    # `_materialise_deps` calls for a DIFFERENT plugin in the SAME process
    # would race on this value (a pre-existing class of risk this module
    # already carries — e.g. `plugins_root()`'s HOME-relative resolution
    # makes the same single-flight-per-process assumption).
    previous_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(tmp_dir)
    try:
        try:
            venv_result = await backend.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                policy, cwd=str(plugin_root),
            )
        except Exception as exc:  # noqa: BLE001 — surface as a materialise error, not a crash
            return None, f"venv creation error: {exc}"
        if venv_result.returncode != 0:
            detail = venv_result.stderr.decode("utf-8", errors="replace").strip()
            # `python -m venv` needs `ensurepip`, which some distros (Debian/
            # Ubuntu's bare `python3`) split into a separate `python3-venv`
            # package — the one narrow case this pip-based materialise trades
            # in for dropping the `uv` dependency (#3202 symptom 2). CPython's
            # own venv module already names the missing package in its stderr
            # ("ensurepip is not available ... apt install python3.X-venv"),
            # so surface a decision-enabling hint on top of the raw detail
            # rather than leaving the operator to parse a stack trace.
            if "ensurepip" in detail.lower():
                return None, (
                    f"venv creation failed (exit {venv_result.returncode}): {detail}\n"
                    "Hint: this Python may be missing ensurepip. On Debian/"
                    "Ubuntu, install the matching venv package (e.g. "
                    "`apt install python3-venv`) and retry."
                )
            return None, f"venv creation failed (exit {venv_result.returncode}): {detail}"

        # Discover the ACTUAL interpreter (see ``_venv_interpreter_path``'s
        # docstring) — this is the ONE place the venv/OS layout is figured
        # out, before either the pip install below or the MCP spawn rewrite
        # use the result.
        try:
            venv_python = _venv_interpreter_path(venv_dir)
        except FileNotFoundError as exc:
            return None, str(exc)

        try:
            install_result = await backend.run(
                [str(venv_python), "-m", "pip", "install",
                 "--cache-dir", str(cache_dir), "-r", str(requirements)],
                policy,
                cwd=str(plugin_root),
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"pip install error: {exc}"
        if install_result.returncode != 0:
            detail = install_result.stderr.decode("utf-8", errors="replace").strip()
            return None, f"pip install failed (exit {install_result.returncode}): {detail}"
        return venv_python, None
    finally:
        if previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmpdir


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
    find these entries again. The write is gated by ``require_file_write``
    on the mcp.yaml path (#3088), mirroring the sibling skill/pipeline
    register steps' own config-write gates."""
    mcp_json = plugin_root / ".mcp.json"
    entries = _build_mcp_entries(mcp_json, venv_python)
    if not entries:
        return []

    from reyn.core.events.config_recovery import record_config_generation
    from reyn.core.op_runtime.mcp_install import probe_mcp_server
    from reyn.runtime.hot_reload import dispatch_install_reload, is_pure_addition

    config_path = _mcp_config_path(project_root)

    # ── Permission gate — mcp.yaml write (#3088). The sibling capability
    # registers in the same step (skills → _skill_install_handle, pipelines →
    # _pipeline_install_handle) each gate their own config write via
    # ``require_file_write``; this mcp register wrote ``.reyn/config/mcp.yaml``
    # directly without one, an asymmetric ungated write on the registration
    # axis (distinct from the global-copy write gate on ``~/.reyn/plugins/``
    # above, which authorizes writing plugin CODE, not the mcp registration).
    # Mirrors skill_install.py:522 / pipeline_install.py:395's shape exactly.
    if ctx.permission_resolver is not None:
        sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=sandbox, bus=ctx.intervention_bus,
        )

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
    # Drop-registry-first for any crashed partial (its dangling entries + copy),
    # then proceed. project_root/state_log/events threaded so the registry-drop
    # half of the rollback actually runs (a bare copy-only sweep would leave
    # dangling registry entries).
    await reconcile_plugin_installs(
        root, project_root=project_root,
        state_log=getattr(ctx, "state_log", None), events=ctx.events,
    )

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
        # ── RUN-CODE TRUST GATE (§3.10 item 3 — the RCE boundary) ──────────────
        # This is the DISTINCT, per-install, never-persisted operator-trust
        # decision for installing + RUNNING remote code — checked BEFORE the
        # fetch, so a declined trust never even reaches the network. It is
        # SEPARATE from require_http_get below: a persistent http.get /
        # web.fetch host approval must NEVER be able to satisfy the run-code
        # decision (else a host approved once for a fetch becomes silent-RCE
        # for every future git plugin). require_http_get still gates the
        # network reachability of the fetch itself (defense in depth), but the
        # run-code trust gate is the one that makes {kind:git} safe.
        if ctx.permission_resolver is not None:
            await ctx.permission_resolver.require_plugin_git_run_code_trust(
                op.source.url, ctx.intervention_bus, ctx.actor,
            )
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
            # #3048: derive the pypi.org dep-fetch approval from the
            # install's OWN write-approval (gate 1, just above) instead of
            # raising a SEPARATE interactive http.get prompt here. Gate 1
            # already required (and received) explicit operator/config
            # consent for THIS install; materialising the plugin's declared
            # deps from the standard package index is intrinsic to
            # "install this plugin", not a distinct capability the operator
            # is separately asked about. Without this, a codeact/headless
            # dispatch (a bus is wired but nothing answers it) awaits this
            # prompt indefinitely and gets guillotined by the caller's
            # compute-budget timeout — #3048's confirmed root cause.
            #
            # SECURITY (confused-deputy guard): the derived grant is scoped
            # to EXACTLY "pypi.org" — the fixed index ``uv pip install``
            # resolves against regardless of what the plugin's
            # requirements.txt names — never a blanket http.get grant. A
            # plugin's install approval must not silently authorise
            # fetching from an arbitrary host; the config-deny and
            # sandbox-network-veto tiers inside require_http_get (checked
            # BEFORE the persisted-approval tier this derive feeds) still
            # apply on top of the derive, so an operator/config-level
            # "http.get.pypi.org: deny" or a network-disabled sandbox
            # policy still blocks materialisation.
            ctx.permission_resolver.session_approve_host(
                "pypi.org", ctx.actor, kind="http.get",
            )
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


def is_registered_plugin_root(plugin_root: Path) -> bool:
    """True iff *plugin_root* (``~/.reyn/plugins/<name>/``) is a COMPLETED
    install — the single source of truth other modules (e.g.
    ``reyn.plugins.body_read``, #3162-adjacent) consult to decide whether a
    plugin's shipped content is operator-approved, install-time-trusted
    content vs. an unreviewed on-disk directory.

    "Registered" means what step 9 of ``handle`` above means by it: the
    completion sidecar (:func:`_read_completed_kind` — written ONLY at step 9,
    after source-resolve → manifest-validate → permission-gated copy →
    capability-register all succeeded) is present, AND no
    ``_install_state.json`` in-progress marker is still sitting there (a
    crashed/interrupted partial — step 0's ``reconcile_plugin_installs``
    rolls these back on the next ``plugin_install`` call, but a caller
    querying in the window before that reconcile runs must not treat the
    stale partial as trustworthy).

    Deliberately NOT keyed off ``skills.yaml``/``pipelines.yaml`` enablement:
    enable/disable is a project-local "use it or don't" toggle over content
    that was already approved once, at install time, into the GLOBAL
    ``~/.reyn/plugins/`` copy (§3.3) — it is not a re-review of the content
    itself, so it must not gate whether that content counts as trusted.
    """
    if not plugin_root.is_dir():
        return False
    if _install_state_path(plugin_root).exists():
        return False
    return _read_completed_kind(plugin_root) is not None


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("plugin_install", handle, canonical=STRUCTURED_PASSTHROUGH)
