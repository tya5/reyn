"""plugin_install kind handler — promote/install a self-contained plugin
directory (ADR 0064 §3.2/§3.8/§3.10, P2 install machinery).

**Register-only** (#3209 — architect-firm redesign, owner GO 2026-07-23):
plugin install registers a plugin's mcp/pipelines/skills.yaml capability
entries; it never provisions the plugin's external Python dependencies.
Dep-fetch was a foreign responsibility (env-provisioning) riding a
registration op — the entire pre-#3209 ``<sys.executable> -m venv`` +
``<venv_python> -m pip install`` materialise step, its two interpreter-path
resolvers, and the ``_deps_materialised`` install-state stage are REMOVED,
clean-break (no transition shim). External deps are now **skill-driven**:
the operator/LLM creates their OWN venv (following the plugin's
``requirements.txt`` + the installing skill's SETUP instructions) and points
the plugin's ``mcp.json`` server ``command`` at that venv's python
interpreter absolute path directly — never a reyn-managed venv. See ADR 0064
§3.11b for the full rationale and the interpreter-path-resolution history
(§3.11a) this redesign supersedes.

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
2. **Load + validate** ``plugin.json`` (plugin root — #4570 conversion A
   relocated it out of ``.reyn-plugin/``) via P1's ``load_plugin_manifest``
   — a missing/malformed manifest refuses BEFORE any copy.
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
7. **Register**: for each capability the manifest declares, call the
   SAME existing register verbs — ``skill_install.handle`` /
   ``pipeline_install.handle`` for skills/pipelines (each op carries
   ``plugin_id=<name>``, §3.7's additive provenance field), and a
   ``require_file_write``-gated (#3088) direct ``.reyn/config/mcp.yaml``
   write (mirrors ``mcp_install_local``'s shape, probe-then-commit) for
   the optional root ``mcp.json``. A server's ``command`` is registered
   AS-IS (no venv-interpreter rewrite) — whatever absolute path the
   plugin's ``mcp.json`` names (or the operator edits in afterward,
   post-#3209) is what spawn execs.
8. **Complete**: delete the ``_install_state.json`` marker (absence =
   completed — the state step 0's reconcile checks) and emit
   ``plugin_install_completed``.

Audit-events emitted (at minimum): ``plugin_install_started`` /
``_copied`` / ``_registered`` / ``_completed``, plus one
``mcp_server_install_skipped`` (#4580) per declared MCP server that a
probe failure or a denied MCP-axis permission gate dropped — see
``_register_mcp``'s own docstring for the shape this closes — and one
``pipeline_install_skipped``/``skill_install_skipped`` (#4590) per
declared pipeline/skill whose OWN sub-install call returned a non-
``"installed"`` status (a bad name, a threat-scan block, a missing DSL
file, ...) rather than raising. Unlike mcp's probe-then-commit (which
skips BEFORE calling the sub-install at all), a pipeline/skill
sub-install always runs — its failure is read from the returned
``status`` field, not a separate pre-check.

**Fail-fast, never runtime-fetch** (#3060 by-construction requirement,
preserved across the #3209 redesign): a server whose ``command`` names an
incomplete/missing venv fails at MCP spawn with a clear OS-level error
(e.g. "no such file or directory") — plugin_install never falls back to
fetching deps at spawn time to paper over that.

**Not WAL-derived** (§3.11): the ``~/.reyn/plugins/`` copies are FILES, not
WAL-event-derived state — the CLAUDE.md truncate-falsify recovery gate does
not apply to them. The reconcile in this module is a filesystem/registry
consistency check; the registry entries THEMSELVES (mcp/pipelines/skills.yaml)
still ride the existing config-generation recovery path via the
sub-handlers they call.

**Concurrency (#3212)**: ``~/.reyn/plugins/`` is DELIBERATELY a single
global, session/workspace-UNSCOPED path (ADR 0064 §3.3 — "install once, use
everywhere"); this is unchanged by #3212. What #3212 fixes is that two
concurrent ``plugin_install``/``plugin_uninstall`` calls (same or different
sessions) racing on the SAME global path could wipe each other's in-flight
work:

  - The step-0 reconcile above previously could not tell a genuinely-crashed
    partial install (marker present, no live owner) from a CONCURRENT
    still-in-progress install of the SAME name (marker present, owner very
    much alive) — both looked identical, so a concurrent reconcile could
    ``rmtree`` a live install mid-copy. Fixed by making the
    ``_install_state.json`` marker carry ``{pid, ts}`` (mirroring
    ``reyn.data.index.build_lock``'s marker shape) and having reconcile check
    liveness via ``build_lock.pid_alive`` PER ENTRY before rolling anything
    back: a live owner is skipped (back off, not wiped), only a dead/missing/
    legacy (no-pid) marker is treated as crashed and rolled back as before.
  - ``plugin_name_lock`` (below) is a blocking, bounded-wait, cross-process
    per-plugin-name advisory lock (same marker-file primitive, reused from
    ``build_lock.py``, but BLOCK-AND-WAIT semantics rather than
    ``build_lock``'s take-or-skip index-build contract — a skipped uninstall
    would surprise the operator) serializing every mutating step of
    ``plugin_install``/``plugin_uninstall`` on the same name, so an install's
    ``copytree`` can never interleave with a concurrent uninstall's
    ``rmtree`` (or another install of the same name). Reconcile's own
    rollback of a dead entry also takes this lock (short timeout) before
    touching it, so the sweep can never race a live mutator either.
  - The content copy itself (``_copy_plugin_tree``) is staged into a unique
    ``~/.reyn/plugins/.staging/<name>-<uuid>/`` dir (same filesystem as the
    final target, so the final move is an atomic ``Path.replace``) and only
    swapped into place at ``plugin_root`` once fully copied — a concurrent
    reader (e.g. ``pipe list`` resolving an absolute registered path) always
    sees either the complete old tree or the complete new tree, never a
    half-copied one. The staging dir carries the SAME liveness marker (it
    IS the eventual ``_install_state.json``, moved into place by the rename),
    so reconcile's ``.staging`` sweep is liveness-aware too, not a blanket
    wipe — it must not delete a concurrent install's in-progress staging.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from uuid import uuid4

from reyn.data.index.build_lock import (
    pid_alive,
    read_lock_holder,
    remove_lock_marker,
    write_lock_marker,
)
from reyn.plugins.manifest import (
    PluginManifestError,
    capability_kinds_present,
    load_plugin_manifest,
)
from reyn.plugins.source import resolve_name_collision
from reyn.plugins.tokens import PluginTokenContext, expand_reyn_tokens, expand_with_map
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
    installs once to global, enablement is project-local). See
    :func:`plugin_data_root` for its SIBLING (``~/.reyn/plugin-data/``) —
    the per-plugin PERSISTENT data directory (#4570 conversion D), kept
    OUTSIDE this directory specifically because this one gets wiped and
    replaced wholesale on every reinstall."""
    return Path.home() / ".reyn" / "plugins"


def plugin_data_root() -> Path:
    """``~/.reyn/plugin-data/`` — the global, per-plugin PERSISTENT
    writable directory the Agent Plugins 1.0 standard's ``${PLUGIN_DATA}``
    token resolves to (#4570 conversion D, lead-coder ruling, 2026-08-13).
    A SIBLING of :func:`plugins_root`, not a subdirectory of it — deliberate:
    step 5 of ``handle`` below replaces ``plugin_root`` (``plugins_root() /
    name``) WHOLESALE on every install/update (an atomic ``.staging``
    swap), so anything a plugin needs to SURVIVE a reinstall cannot live
    inside it. Global scope (not project-scoped, i.e. not under a
    project's own ``.reyn/``) mirrors ``plugins_root()``'s own ADR §3.3
    rationale: the CODE installs once globally; a project-scoped data
    dir would mean the SAME globally-installed plugin quietly gets a
    DIFFERENT data directory per enabling project, which nothing in the
    standard's own "clients provide a persistent writable PLUGIN_DATA
    directory" wording implies.

    ``plugin_uninstall`` deliberately never deletes anything under here
    (lead-coder ruling: data outliving code is the safe direction; the
    reverse is unrecoverable) — see that module's own disclosure line."""
    return Path.home() / ".reyn" / "plugin-data"


# ---------------------------------------------------------------------------
# Per-plugin-name cross-process lock (#3212 layer b)
# ---------------------------------------------------------------------------

_LOCK_POLL_INTERVAL_S = 0.05
DEFAULT_PLUGIN_LOCK_TIMEOUT_S = 30.0


def _locks_dir(root: Path) -> Path:
    return root / ".locks"


def _plugin_lock_path(root: Path, name: str) -> Path:
    return _locks_dir(root) / f"{name}.lock"


@contextlib.asynccontextmanager
async def plugin_name_lock(
    name: str,
    root: "Path | None" = None,
    *,
    timeout: float = DEFAULT_PLUGIN_LOCK_TIMEOUT_S,
):
    """Blocking, bounded-wait, cross-process advisory lock serializing every
    MUTATING step of ``plugin_install``/``plugin_uninstall`` on the same
    plugin *name* (#3212 layer b) — so an install's ``copytree`` can never
    interleave with a concurrent uninstall's ``rmtree`` (or another install
    of the same name).

    Reuses ``reyn.data.index.build_lock``'s ``{pid, ts}`` marker-file
    primitives (``pid_alive`` / ``read_lock_holder`` / ``write_lock_marker`` /
    ``remove_lock_marker``) for the atomic take + staleness check, but with
    **BLOCKING wait** semantics bounded by *timeout* seconds — NOT
    ``build_lock``'s take-or-skip contract (a skipped uninstall would
    surprise the operator; here the caller genuinely needs the mutation to
    happen, just serialized). A stale lock (dead holder pid) is reclaimed
    immediately rather than waited out.

    Raises ``TimeoutError`` if *timeout* elapses with a live holder still
    present.
    """
    base = root if root is not None else plugins_root()
    locks_dir = _locks_dir(base)
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _plugin_lock_path(base, name)

    def _take_atomic() -> bool:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return False
        os.close(fd)
        write_lock_marker(lock_path)
        return True

    deadline = time.monotonic() + timeout
    while True:
        if _take_atomic():
            break
        holder_pid = read_lock_holder(lock_path)
        if holder_pid is None or not pid_alive(holder_pid):
            # Stale/legacy/dead lock — reclaim immediately, no waiting.
            remove_lock_marker(lock_path)
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {timeout}s waiting for the plugin lock on "
                f"{name!r} (held by pid {holder_pid})"
            )
        await asyncio.sleep(_LOCK_POLL_INTERVAL_S)

    try:
        yield
    finally:
        remove_lock_marker(lock_path)


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


def _write_install_state(plugin_root: Path, kind: str, *, pid: "int | None" = None) -> None:
    """Write the in-progress marker, carrying ``{pid, ts}`` (#3212) so
    ``reconcile_plugin_installs`` can tell a genuinely-crashed partial (dead
    pid) from a concurrent still-in-progress install of the SAME name (live
    pid) — both previously looked identical (marker present), which is the
    #3212 root cause. ``pid`` defaults to the CURRENT process
    (``os.getpid()``, the real production path); callers simulating a crash
    for tests pass an explicit dead pid."""
    state_path = _install_state_path(plugin_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "name": plugin_root.name, "kind": kind, "status": "installing",
            "pid": pid if pid is not None else os.getpid(),
            "ts": time.time(),
        }),
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

    **Liveness-aware (#3212 layer a)**: a marker whose ``pid`` is ALIVE means
    a CONCURRENT install of this same name is still in progress — not a
    crashed partial — so it is SKIPPED (back off, do not wipe) rather than
    rolled back. Only a dead / missing / legacy (no-``pid`` field) marker is
    treated as a crashed partial and rolled back, same as before #3212. Each
    rollback additionally takes the #3212 per-name ``plugin_name_lock``
    (short timeout) before touching the entry, so this sweep can never race
    a live ``plugin_install``/``plugin_uninstall`` mutator either — if the
    lock cannot be acquired promptly (some other mutator just grabbed it),
    the entry is left for the next reconcile pass rather than forced.
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
        marker_pid = state.get("pid")
        if isinstance(marker_pid, int) and marker_pid > 0 and pid_alive(marker_pid):
            # A concurrent install of THIS name is still in progress — not a
            # crashed partial. Skip; do not wipe a live install (#3212).
            continue
        # Drop-registry-FIRST (§3.11): remove any entries this partial install
        # registered before deleting the copy they point at. Guarded by the
        # per-name lock so this rollback cannot interleave with a concurrent
        # mutator that just took ownership of this name.
        try:
            async with plugin_name_lock(entry.name, base, timeout=5.0):
                if project_root is not None:
                    await _reconcile_drop_registry_entries(
                        project_root, entry.name, state_log=state_log, events=events,
                    )
                shutil.rmtree(entry, ignore_errors=True)
                rolled_back.append(entry.name)
                if events is not None:
                    events.emit(
                        "plugin_install_reconciled", name=entry.name, action="rolled_back",
                    )
        except TimeoutError:
            # Someone else holds this name's lock right now — leave it for
            # the next reconcile pass rather than forcing the rollback.
            continue
    # A staging dir (git-clone OR the atomic-copy staging, #3212 layer c) is
    # never "installed" under any name. Atomic-copy staging carries the SAME
    # ``_install_state.json`` marker it will be renamed into place with, so
    # it is liveness-checked the same way as a top-level entry above — a
    # concurrent install's in-progress staging must not be swept. Git-clone
    # staging carries no such marker and is always safe to sweep (its window
    # is a single synchronous clone call, never left half-formed across
    # reconcile passes under a live owner).
    staging = base / ".staging"
    if staging.is_dir():
        for child in sorted(staging.iterdir()):
            if not child.is_dir():
                continue
            child_state = _read_install_state(child)
            child_pid = child_state.get("pid") if child_state else None
            if isinstance(child_pid, int) and child_pid > 0 and pid_alive(child_pid):
                continue
            shutil.rmtree(child, ignore_errors=True)
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


#: #4610: two DIFFERENT token vocabularies now apply per file, a
#: necessary consequence of #4570 conversion D ending the pre-D
#: uniformity on purpose (mcp.json takes the Agent Plugins 1.0 standard's
#: own ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` names, field-aware;
#: pipelines/SKILL.md keep the reyn-native ``${REYN_PLUGIN_ROOT}``
#: full-text bake — see ``_bake_mcp_json_fields``'s own docstring for
#: why receiving BOTH names in both places was rejected). A wrong guess
#: previously stayed silently literal with no error, install still
#: reporting success, until the server/skill actually ran (#4364 C-1's
#: same "declaration and reality silently drift" shape). These two
#: regexes are what the warning scan below checks for — NEVER the
#: FIX (adding a second recognized name), only the DISCLOSURE.
_REYN_TOKEN_RE = re.compile(r"\$\{REYN_\w+\}")
_SPEC_TOKEN_RE = re.compile(r"\$\{PLUGIN_(?:ROOT|DATA)\}")


def _stale_token_warnings(text: str, pattern: "re.Pattern[str]", location: str, hint: str) -> "list[str]":
    """#4610: every DISTINCT wrong-vocabulary token still literally
    present in *text* after its file's own bake pass — named by
    *location* (which file/field this token survived in) with *hint*
    (which name THIS location actually expands). Never raises, never
    blocks the install (D-2-shaped: report-only) — the caller threads
    the result into the install's own return value, the same disclosure
    shape #4601's ``plugin_data_retained_at`` and #4580's ``skipped``
    already established for this module."""
    found = sorted(set(pattern.findall(text)))
    if not found:
        return []
    names = ", ".join(found)
    return [f"{location}: found {names}, never expanded there — {hint}"]


def _expand_plugin_files(plugin_root: Path, token_ctx: PluginTokenContext) -> "list[str]":
    """Bake stable-location tokens into every text file a capability might
    read (§3.4/§3.5): the root ``mcp.json`` (#4570 conversion C1 —
    renamed from ``.mcp.json``, the Agent Plugins 1.0 canonical filename;
    #4570 conversion D — FIELD-AWARE bake, see :func:`_bake_mcp_json_fields`),
    every ``pipelines/*.yaml`` (STILL the reyn-native ``${REYN_*}`` full-text
    bake — pipelines are a reyn extension the standard doesn't define, #4570
    conversion D deliberately does not touch this candidate), and every
    ``skills/*/SKILL.md``. Non-existent globs are simply empty — every
    capability is optional (§3.1).

    Returns every #4610 stale-token warning collected across all three —
    a plugin author's mistaken use of the WRONG file's token vocabulary
    (``${PLUGIN_ROOT}`` in a pipeline, ``${REYN_PLUGIN_ROOT}`` in
    mcp.json's args/env/cwd) surviving literally with no error otherwise."""
    warnings: list[str] = []
    warnings.extend(_bake_mcp_json_fields(plugin_root / "mcp.json", token_ctx))
    pipelines_dir = plugin_root / "pipelines"
    if pipelines_dir.is_dir():
        for path in pipelines_dir.glob("*.yaml"):
            warnings.extend(_bake_all_tokens(path, token_ctx))

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
            warnings.extend(_bake_plugin_root_only(path, token_ctx.plugin_root))
    return warnings


def _bake_all_tokens(path: Path, token_ctx: PluginTokenContext) -> "list[str]":
    """Expand every ``${REYN_*}`` token *token_ctx* carries a value for, in
    place — the pipeline copy-time bake (#4570 conversion D: mcp.json no
    longer goes through this function — see :func:`_bake_mcp_json_fields`
    — pipelines are a reyn extension the Agent Plugins 1.0 standard
    doesn't define, so they keep the pre-#3070 full-text/``${REYN_*}``
    behavior unchanged).

    #4610: after expansion, a literal ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}``
    surviving in the result is the WRONG vocabulary for this file (that
    pair is mcp.json's own spec-canonical name, meaningless here) —
    disclosed, never silently accepted, never auto-corrected (this
    function only ever expands ``${REYN_*}`` — receiving the spec's own
    names too was rejected, see :func:`_bake_mcp_json_fields`'s
    docstring)."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    expanded = expand_reyn_tokens(text, token_ctx)
    if expanded != text:
        path.write_text(expanded, encoding="utf-8")
    return _stale_token_warnings(
        expanded, _SPEC_TOKEN_RE, str(path),
        "this file expands ${REYN_PLUGIN_ROOT}/${REYN_PROJECT_DIR}/${REYN_SKILL_DIR}, not ${PLUGIN_ROOT}/${PLUGIN_DATA} (that pair is mcp.json's own vocabulary)",
    )


def _bake_mcp_json_fields(path: Path, token_ctx: PluginTokenContext) -> "list[str]":
    """Field-aware ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` bake for
    ``mcp.json`` (#4570 conversion D) — expand ONLY ``args`` / ``env``
    values / ``cwd``, NEVER ``command`` or ``url``.

    #4610: after expansion, a literal ``${REYN_*}`` token surviving in
    args/env/cwd is the WRONG vocabulary for this file (mcp.json's own
    field-aware bake only ever recognizes ``${PLUGIN_ROOT}``/
    ``${PLUGIN_DATA}``) — disclosed, never silently accepted. A
    ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` surviving inside ``command``/
    ``url`` is CORRECT and never warned about — that is the spec's own
    exclusion this function exists to honor, not a mistake.

    ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` is the Agent Plugins 1.0
    mcp.schema.json's OWN token vocabulary for mcp.json specifically —
    NOT ``${REYN_PLUGIN_ROOT}`` (the reyn-native name pipelines/SKILL.md
    still use, untouched by this conversion). Deliberately a SEPARATE,
    narrower map from ``tokens.py``'s ``PluginTokenContext.tokens()`` —
    this is the spec's own canonical names, scoped to the one file the
    spec defines them for.

    This is the spec's own exclusion (mcp.schema.json's token-expansion
    note, measured directly from the published schema, #4570): a
    plugin-authored string must not be able to choose WHAT gets executed
    (``command``) or WHERE a network request goes (``url``) via a
    token whose VALUE this module resolves — only WHICH arguments/env/cwd
    it runs with. Before this conversion, ``_bake_all_tokens`` did a
    blind full-text replace across the WHOLE file, expanding inside
    ``command``/``url`` too (architect's own #4570 finding: reyn's
    pre-conversion behavior was MORE permissive than the standard it is
    aligning with, not merely differently-spelled).

    ``${PLUGIN_DATA}`` resolves to :func:`plugin_data_root` / *the
    plugin's own name* (lead-coder ruling, #4570) — created here,
    eagerly, on every bake (mirrors the spec's own "clients PROVIDE a
    persistent writable PLUGIN_DATA directory" wording: existence is
    reyn's responsibility, not something a plugin's own first-run code
    must ``mkdir -p`` for itself).

    Parses the file as JSON (not text) so field identity is unambiguous
    — a token that happens to appear inside a ``command``/``url`` string
    is left LITERAL, not a coincidental string match away from being
    expanded anyway."""
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return []
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    plugin_data_dir = plugin_data_root() / token_ctx.plugin_root.name
    plugin_data_dir.mkdir(parents=True, exist_ok=True)
    token_map = {
        "PLUGIN_ROOT": str(token_ctx.plugin_root),
        "PLUGIN_DATA": str(plugin_data_dir),
    }
    changed = False
    warnings: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        args = spec.get("args")
        if isinstance(args, list):
            new_args = [
                expand_with_map(a, token_map) if isinstance(a, str) else a for a in args
            ]
            if new_args != args:
                spec["args"] = new_args
                changed = True
            warnings.extend(
                _stale_token_warnings(
                    " ".join(str(a) for a in new_args), _REYN_TOKEN_RE,
                    f"{path} ({name}.args)",
                    "mcp.json expands ${PLUGIN_ROOT}/${PLUGIN_DATA}, not ${REYN_*} names (those are pipelines/SKILL.md's own vocabulary)",
                )
            )
        env = spec.get("env")
        if isinstance(env, dict):
            new_env = {
                k: (expand_with_map(v, token_map) if isinstance(v, str) else v)
                for k, v in env.items()
            }
            if new_env != env:
                spec["env"] = new_env
                changed = True
            warnings.extend(
                _stale_token_warnings(
                    " ".join(str(v) for v in new_env.values()), _REYN_TOKEN_RE,
                    f"{path} ({name}.env)",
                    "mcp.json expands ${PLUGIN_ROOT}/${PLUGIN_DATA}, not ${REYN_*} names (those are pipelines/SKILL.md's own vocabulary)",
                )
            )
        cwd = spec.get("cwd")
        if isinstance(cwd, str):
            new_cwd = expand_with_map(cwd, token_map)
            if new_cwd != cwd:
                spec["cwd"] = new_cwd
                changed = True
            warnings.extend(
                _stale_token_warnings(
                    new_cwd, _REYN_TOKEN_RE, f"{path} ({name}.cwd)",
                    "mcp.json expands ${PLUGIN_ROOT}/${PLUGIN_DATA}, not ${REYN_*} names (those are pipelines/SKILL.md's own vocabulary)",
                )
            )
        # `command` and `url` are NEVER touched or warned about — see
        # this function's own docstring.
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return warnings


def _bake_plugin_root_only(path: Path, plugin_root: Path) -> "list[str]":
    """Expand ONLY ``${REYN_PLUGIN_ROOT}`` in *path*, in place — every other
    ``${REYN_*}``/``${CLAUDE_*}``/``${env:...}`` token is left as a literal
    string for the invocation-time skill-load pass. A targeted string
    replace rather than ``expand_reyn_tokens`` (whose ``PluginTokenContext``
    requires ``project_dir``, which this call must NOT supply a baked value
    for — see the caller's docstring).

    #4610: a literal ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` surviving here
    was NEVER valid in a SKILL.md (that pair is mcp.json's own spec
    vocabulary) — disclosed as a stale-token warning, same as the
    pipeline/mcp.json bakes."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    expanded = text.replace("${REYN_PLUGIN_ROOT}", str(plugin_root))
    if expanded != text:
        path.write_text(expanded, encoding="utf-8")
    return _stale_token_warnings(
        expanded, _SPEC_TOKEN_RE, str(path),
        "this file expands ${REYN_PLUGIN_ROOT} only, not ${PLUGIN_ROOT}/${PLUGIN_DATA} (that pair is mcp.json's own vocabulary)",
    )


def _mcp_config_path(project_root: Path) -> Path:
    return project_root / ".reyn" / "config" / "mcp.yaml"


def _build_mcp_entries(mcp_json: Path) -> dict:
    """Parse the plugin's root ``mcp.json`` (#4570 conversion C1 — renamed
    from ``.mcp.json``; standard shape,
    ``{"mcpServers": {"<name>": {"type", "command", "args", "env"?, "url"?}}}``)
    into reyn's ``mcp.servers.<name>`` entry shape.

    **Transport-type ADAPTER (#4570 conversion C1, temporary)**: the
    Agent Plugins 1.0 canonical mcp.schema.json spells the HTTP-family
    transport ``"streamable-http"``; reyn's OWN ``.reyn/config/mcp.yaml``
    vocabulary still says ``"http"`` (``mcp/client.py``'s
    ``_SUPPORTED_TYPES``) — that repo-wide rename is split off into #4604
    (conversion C2, deliberately NOT done here: 18 src files + 9 test
    files outside the plugin subsystem, lead-coder ruling). This function
    is the ONE translation point: a spec-legal ``"streamable-http"``
    becomes reyn-internal ``"http"``; ``"sse"`` passes through UNCHANGED
    (it is a DISTINCT value in both vocabularies — mapping it into
    ``"http"`` too would be a real bug, not just an incomplete rename).
    Remove this translation once #4604 lands and reyn's own vocabulary
    says ``"streamable-http"`` natively.

    ``command`` is registered AS-IS (#3209 — register-only redesign: no
    venv-interpreter rewrite here any more). A plugin whose server needs a
    Python env other than the ambient ``python``/``python3`` on ``PATH``
    names an absolute interpreter path directly in its ``mcp.json`` (per
    its skill's SETUP instructions — the operator/LLM creates that venv
    themselves), or the operator edits the registered entry's ``command``
    afterward."""
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
            spec_type = spec.get("type", "streamable-http")
            reyn_type = "http" if spec_type == "streamable-http" else spec_type
            entry: dict = {"type": reyn_type, "url": spec["url"]}
        else:
            entry = {
                "type": "stdio",
                "command": str(spec.get("command", "")),
                "args": [str(a) for a in spec.get("args", [])],
            }
        env = spec.get("env")
        if isinstance(env, dict):
            entry["env"] = {str(k): str(v) for k, v in env.items()}
        out[str(name)] = entry
    return out


async def _register_mcp(
    plugin_root: Path, plugin_name: str,
    ctx: OpContext, project_root: Path,
) -> dict:
    """Register every server declared in the plugin's root ``mcp.json``
    into ``.reyn/config/mcp.yaml`` — mirrors ``mcp_install_local``'s shape
    (probe-then-commit on a live per-session reloader; deferred write
    otherwise), tagged with ``plugin_id`` (§3.7) so ``plugin_uninstall`` can
    find these entries again. The write is gated by ``require_file_write``
    on the mcp.yaml path (#3088), mirroring the sibling skill/pipeline
    register steps' own config-write gates.

    #4580: returns ``{"registered": [<name>, ...], "skipped": [{"name":
    ..., "reason": ...}, ...]}`` — before this, a DECLARED server that
    failed its probe (or was denied at the MCP-axis permission gate) was
    silently dropped: no event, not in the return value, no count
    anywhere the operator (or a test) could see the difference between
    "declared 3, registered 3" and "declared 3, registered 2". The
    doc-facing instruction a bundled plugin's own ``requirements.txt``
    gives ("point the registered ``command`` at your venv's interpreter")
    presupposes an entry exists to point AT — when the probe never
    passes, none was ever written, and install still reported success.
    Each skip also gets its own ``mcp_server_install_skipped`` audit-event
    (``reason`` = ``"probe_failed"`` or ``"permission_denied"``) — never a
    silent ``continue``. This does NOT make the server work (#3209: reyn
    does not manage venvs) — it only makes the drop visible."""
    mcp_json = plugin_root / "mcp.json"
    entries = _build_mcp_entries(mcp_json)
    if not entries:
        return {"registered": [], "skipped": []}

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
    skipped: list[dict] = []
    for name, entry in entries.items():
        is_addition = is_pure_addition(name, servers)
        reloader = getattr(ctx, "hot_reloader", None)
        if is_addition and reloader is not None:
            try:
                probe_err = await probe_mcp_server(
                    name, entry, agent_id=getattr(ctx, "agent_id", None),
                    cancel_event=getattr(ctx, "cancel_event", None),
                    # #3552: this is the THIRD sibling call site sharing
                    # probe_mcp_server's pre-#3552 hole (a live connection to a
                    # plugin-declared server name before mcp.yaml is
                    # authoritative, with no MCP-axis gate at all). Threading
                    # the resolver/bus/contextual here is what turns the fix
                    # on for plugin-bundled MCP registration too.
                    permission_resolver=ctx.permission_resolver,
                    bus=ctx.intervention_bus,
                    contextual=getattr(ctx, "contextual_permission", None),
                )
            except PermissionError:
                # #4580: a denied MCP-axis gate is treated the same AS AN
                # OUTCOME as any other probe failure — skip this one server
                # (nothing written for it), not the whole plugin install.
                # Previously a bare ``continue`` here: the operator saw a
                # decision-enabling deny at the GATE (require_mcp's own
                # message), but nothing on the INSTALL RESULT side recorded
                # that this declared server never made it in — a different
                # surface than the one the comment used to justify silence
                # by pointing at.
                skipped.append({"name": name, "reason": "permission_denied"})
                continue
            if probe_err is not None:
                # #4580: probe-then-commit — skip this one server (nothing
                # written for it) rather than fail the whole plugin install;
                # other capabilities may still be perfectly usable. Now
                # RECORDED rather than silently dropped (see this function's
                # own docstring for the full "declared but never registered"
                # shape this closes).
                skipped.append({"name": name, "reason": "probe_failed", "error": str(probe_err)})
                continue
        entry["plugin_id"] = plugin_name
        servers[name] = entry
        registered.append(name)

    for skip in skipped:
        ctx.events.emit(
            "mcp_server_install_skipped", server_id=skip["name"], server_name=skip["name"],
            reason=skip["reason"], source=f"plugin_install:{plugin_name}",
        )

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
            getattr(ctx, "hot_reloader", None), source="mcp_install_local",
            is_addition=True,
            # #3636: names the server(s) this call registered so it doesn't render
            # as an indistinguishable repeat of another mcp_install_local reload.
            detail=", ".join(registered),
        )
    return {"registered": registered, "skipped": skipped}


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

    # ── 3.–8. Per-name-locked mutation (#3212 layer b) ────────────────────────
    # Everything from the name-collision check (which READS plugin_root's
    # current state) through completion is serialized against any concurrent
    # plugin_install/plugin_uninstall of the SAME name — so a collision
    # decision, the copy, and the register/complete steps all observe (and
    # leave) a consistent state, and a concurrent uninstall's rmtree can never
    # interleave with this copy.
    async with plugin_name_lock(safe_name, root):
        # ── 3. Name-collision precedence (§3.8/§3.10) ─────────────────────────
        existing_state = _read_install_state(plugin_root)
        existing_kind = None
        if plugin_root.is_dir() and existing_state is None:
            # A completed prior install has no _install_state.json marker
            # (cleared on success) — its own kind is recorded in a lightweight
            # sidecar written alongside the manifest at registration time
            # (below), since the manifest itself carries no source-kind field.
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

        # ── 4. Permission gate 1 — global-copy write outside the workspace ────
        if ctx.permission_resolver is not None:
            sandbox = _sandbox_policy_from_ctx(ctx)
            await ctx.permission_resolver.require_file_write(
                ctx.permission_decl, str(plugin_root), ctx.actor,
                sandbox_policy=sandbox, bus=ctx.intervention_bus,
            )

        # ── 5. Copy — atomic temp-then-rename (#3212 layer c) ──────────────────
        # Build the full new tree in a UNIQUE staging dir under the same
        # ~/.reyn/plugins/ filesystem (so the final swap is an atomic
        # Path.replace), carrying the SAME _install_state.json marker it will
        # be renamed into place with (reconcile's liveness check above applies
        # to it unchanged, whether it is still under .staging/ or has already
        # been renamed to plugin_root). A concurrent reader resolving
        # plugin_root's absolute path always sees either the complete old tree
        # or the complete new one — never a half-copied one.
        atomic_staging = root / ".staging" / f"{safe_name}-{uuid4().hex}"
        atomic_staging.mkdir(parents=True, exist_ok=True)
        _write_install_state(atomic_staging, source_kind)
        _copy_plugin_tree(source_dir, atomic_staging)
        if staging_cleanup:
            shutil.rmtree(staging_cleanup, ignore_errors=True)
        if plugin_root.exists():
            # Updating an existing install: replace it wholesale (clean
            # atomic swap) rather than merging over stale files the new
            # source no longer carries.
            shutil.rmtree(plugin_root, ignore_errors=True)
        atomic_staging.replace(plugin_root)
        ctx.events.emit("plugin_install_copied", name=safe_name, plugin_root=str(plugin_root))

        # ── 6. Expand ${REYN_*} stable-location tokens ─────────────────────────
        token_ctx = PluginTokenContext(plugin_root=plugin_root, project_dir=project_root)
        # #4610: report-only, never blocks the install — a plugin author's
        # wrong-vocabulary token guess (${PLUGIN_ROOT} in a pipeline,
        # ${REYN_PLUGIN_ROOT} in mcp.json's args/env/cwd) stays literal
        # either way; this is the DISCLOSURE that used to be silent.
        stale_token_warnings = _expand_plugin_files(plugin_root, token_ctx)
        # #4610 follow-up: the result-dict field above is discoverable only
        # by the caller of THIS install call — a plugin installed once and
        # never touched again would have its stale token silently drop out
        # of view the moment the return value isn't kept. The audit-event
        # is the durable record (P6 band member), same "install-time,
        # discrete, named condition" class as `mcp_server_install_skipped`
        # (#4580) / `pipeline_install_skipped`/`skill_install_skipped`
        # (#4590) — one event per finding, not one per install, so a
        # consumer filtering by kind sees exactly the findings, not a
        # count it has to re-derive.
        for warning in stale_token_warnings:
            ctx.events.emit(
                "plugin_install_token_vocabulary_mismatch",
                name=safe_name, warning=warning,
            )

        # ── 7. Register capabilities (#3209: register-only — no dep materialise) ──
        # #4570 conversion B: capability presence is now derived PURELY
        # from directory/file existence, never a manifest-declared list —
        # the manifest no longer carries `capabilities` at all (lead-coder
        # ruling, #4570: the `entries` explicit-subset feature had 0
        # production readers and 0 declaring manifests, so it — and the
        # `capabilities` field it lived on — is DROPPED, not relocated
        # into `extensions["dev.reyn"]`; a manifest still declaring it is
        # a typed-error rejection at parse time, `PluginManifest`'s own
        # `_reject_removed_capabilities_key`). Each sub-install below now
        # always RUNS when its directory/file exists — no per-kind branch
        # to skip, no `entries` ternary to narrow which files get picked
        # up (discover-everything is the only mode left).
        registered: dict[str, list] = {"mcp": [], "pipelines": [], "skills": []}
        # #4590: declared-vs-registered diff, per capability kind. Pipelines
        # and skills DO have a drop path — unlike mcp's probe-then-commit
        # (which skips BEFORE ever calling the sub-install), a pipeline/skill
        # sub-install always runs and can itself fail (bad name, threat-scan
        # block, missing file, ...), returning ``{"status": "error"/"blocked",
        # ...}`` rather than raising (#4580's own comment here previously
        # said "no probe-then-commit step that can drop one, so there is
        # nothing for this axis to record" — the first half is true, the
        # second was wrong: a sub-install's own failure IS a drop, just via
        # a different mechanism than mcp's probe). Before this fix, EVERY
        # sub_result — success or failure — was appended to ``registered``
        # unconditionally, so a failed pipeline/skill install still read as
        # registered, and the new "skipped" key (#4580) reported 0 for both
        # axes regardless of how many actually failed — worse than silence
        # (#4580 dropped quietly; this claimed success for a drop).
        skipped: dict[str, list] = {"mcp": [], "pipelines": [], "skills": []}

        # mcp: _register_mcp already no-ops gracefully when `mcp.json` is
        # absent (returns {"registered": [], "skipped": []}) — safe to call
        # unconditionally, same as every other install regardless of
        # whether this plugin ships an mcp server.
        mcp_result = await _register_mcp(plugin_root, safe_name, ctx, project_root)
        registered["mcp"] = mcp_result["registered"]
        skipped["mcp"] = mcp_result["skipped"]

        pipelines_dir = plugin_root / "pipelines"
        if pipelines_dir.is_dir():
            for dsl_file in sorted(pipelines_dir.glob("*.yaml")):
                sub_op = PipelineInstallIROp(
                    kind="pipeline_install", path=str(dsl_file), plugin_id=safe_name,
                )
                sub_result = await _pipeline_install_handle(sub_op, ctx)
                if sub_result.get("status") == "installed":
                    registered["pipelines"].append(sub_result)
                else:
                    skipped["pipelines"].append(sub_result)
                    ctx.events.emit(
                        "pipeline_install_skipped",
                        plugin_id=safe_name, path=str(dsl_file),
                        reason=sub_result.get("status", "error"),
                        error=sub_result.get("error", ""),
                    )

        skills_dir = plugin_root / "skills"
        if skills_dir.is_dir():
            for skill_dir in sorted(p for p in skills_dir.glob("*") if p.is_dir()):
                sub_op = SkillInstallIROp(
                    kind="skill_install", path=str(skill_dir), plugin_id=safe_name,
                )
                sub_result = await _skill_install_handle(sub_op, ctx)
                if sub_result.get("status") == "installed":
                    registered["skills"].append(sub_result)
                else:
                    skipped["skills"].append(sub_result)
                    ctx.events.emit(
                        "skill_install_skipped",
                        plugin_id=safe_name, path=str(skill_dir),
                        reason=sub_result.get("status", "error"),
                        error=sub_result.get("error", ""),
                    )

        ctx.events.emit("plugin_install_registered", name=safe_name, registered=registered)

        # ── 8. Complete ────────────────────────────────────────────────────────
        _clear_install_state(plugin_root)
        _write_completed_kind(plugin_root, source_kind)
        ctx.events.emit("plugin_install_completed", name=safe_name)

        return {
            "status": "installed",
            "name": safe_name,
            "plugin_root": str(plugin_root),
            "source_kind": source_kind,
            # #4570 conversion B: derived from directory/file existence —
            # the SAME derivation discovery.py's listing uses
            # (capability_kinds_present), so "declared" and "listed" can
            # never independently drift.
            "capabilities": sorted(capability_kinds_present(plugin_root)),
            "registered": registered,
            "skipped": skipped,  # #4580: declared-but-dropped, per capability kind
            # #4610: report-only disclosure — a plugin's wrong-file token
            # guess (${PLUGIN_ROOT} in a pipeline, ${REYN_PLUGIN_ROOT} in
            # mcp.json's args/env/cwd) stayed literal either way; this is
            # what used to be silent. Empty when the bake found nothing
            # stale, same "present but empty" convention as `skipped`.
            "stale_token_warnings": stale_token_warnings,
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
