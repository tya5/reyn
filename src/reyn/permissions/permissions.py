"""
Skill-level permission declarations and approval resolution.

Default grants (no declaration needed):
  file read/glob/grep  — any path within the project root (CWD)
  file write/edit/delete — under project/.reyn/ or project/reyn/ only

Outside the defaults → the skill must declare the path AND the user must approve:
  file.read:  [{path: <path>, scope: just_path|recursive}]   (paths outside CWD)
  file.write: [{path: <path>, scope: just_path|recursive}]
  shell      — declare permissions.shell: true
  mcp        — declare permissions.mcp: [server_name, ...]
  tool       — declare permissions.tool: [tool_name, ...]

Approval choices (shown once at startup before execution starts):
  [y]es                        — allow for this run only
  [j]ust this path always      — persist approval for this exact path + skill
  [r]ecursive from parent      — persist approval for the parent directory + skill (covers all files under it)
  [N]o                         — deny

Approval keys are skill-scoped to prevent external skill privilege escalation:
  "{skill_name}/file.write/{path}"   (just_path)
  "{skill_name}/file.write/{dir}/"   (recursive, trailing slash signals recursive)

Config pre-approval (reyn.yaml / reyn.local.yaml):
  permissions:
    shell: allow
    file.write: allow   # grants all write-class ops for all skills
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ClassVar

from reyn.intervention_choices import (
    ALWAYS,
    JUST_PATH,
    NEVER,
    RECURSIVE,
    YES,
    file_access_choices,
    generic_yn_choices,
    python_choices,
)
from reyn.user_intervention import (
    RequestBus,
    UserIntervention,
)

if TYPE_CHECKING:
    from .models import Skill


_DEFAULT_WRITE_ZONES = (".reyn", "reyn")

# #571 collapse arc Phase 2: canonical paths whose write is gated by a
# specific op handler (= mcp_install / mcp_drop_server / cron_register /
# index_drop) AND therefore must not be silently default-zone-allowed.
# Skills that legitimately need to mutate these must declare them
# explicitly via ``file.write: [{path: ...}]`` (or via the bool-axis
# compat shim that auto-expands into the equivalent file.write entry —
# see ``PermissionDecl._compat_expand_bool_axes``).
#
# Why exempt these specific paths only: they back capability that the
# OS treats as a distinct event-emit + audit-trail surface (server
# install / cron register / index drop). Letting any safe-mode python
# step write them via the broad ``.reyn/`` default zone bypasses the
# corresponding gate. The narrow exception preserves the broad
# ``.reyn/`` write zone for everything else (= chunkers, cursors,
# scratch state).
_CANONICAL_PROTECTED_WRITE_PATHS = (
    ".reyn/mcp.yaml",
    ".reyn/cron.yaml",
    ".reyn/index/sources.yaml",
)


def _normalize_paths(v: object) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _expand(path_str: str) -> Path:
    """Expand ~ and resolve a path string to an absolute Path."""
    return Path(path_str).expanduser().resolve()


def _is_canonical_protected_write(path_str: str) -> bool:
    """Return True if ``path_str`` resolves to one of the #571 protected paths."""
    base = Path.cwd()
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    for rel in _CANONICAL_PROTECTED_WRITE_PATHS:
        if resolved == (base / rel).resolve():
            return True
    return False


def _in_default_write_zone(path_str: str) -> bool:
    """Return True if path falls within a default-granted write zone (.reyn/ or reyn/).

    Exception: canonical paths gated by specific op handlers (#571
    collapse arc Phase 2 — ``.reyn/mcp.yaml`` / ``.reyn/cron.yaml`` /
    ``.reyn/index/sources.yaml``) return False here so the corresponding
    skill / op handler is forced to declare the explicit ``file.write``
    entry (= or the bool-axis compat shim expands to it).
    """
    if _is_canonical_protected_write(path_str):
        return False
    base = Path.cwd()
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    for zone in _DEFAULT_WRITE_ZONES:
        try:
            resolved.relative_to((base / zone).resolve())
            return True
        except ValueError:
            pass
    return False


def _in_default_read_zone(path_str: str) -> bool:
    """Return True if path falls within the default-granted read zone (CWD)."""
    base = Path.cwd()
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        return False


def _decl_covers_path(entries: list[dict], path: str) -> bool:
    """Whether the skill's declared file_read / file_write entries cover ``path``.

    Used by ``require_file_read`` / ``require_file_write`` in non-interactive
    mode to honor skill declarations directly (FP-0008 PR-H, 2026-05-28).

    Matching rules:

    - ``path == "*"`` in any entry → wildcard, matches anything.
    - Empty / missing entry path → skip the entry.
    - ``scope: "recursive"`` → matches the declared path itself OR any
      descendant resolved-path.
    - ``scope: "just_path"`` (default) → matches the resolved declared path
      exactly.

    Returns False on any resolution error so the caller can fall through
    to the standard PermissionError.
    """
    if not entries:
        return False
    try:
        p_resolved = Path(path).expanduser().resolve()
    except Exception:
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        decl_path = entry.get("path", "")
        scope = entry.get("scope", "just_path")
        if not decl_path:
            continue
        if decl_path == "*":
            return True
        try:
            decl_resolved = Path(decl_path).expanduser().resolve()
        except Exception:
            continue
        if scope == "recursive":
            if p_resolved == decl_resolved:
                return True
            try:
                p_resolved.relative_to(decl_resolved)
                return True
            except ValueError:
                pass
        else:  # just_path
            if p_resolved == decl_resolved:
                return True
    return False


@dataclass
class PythonPermission:
    """Per-(module, function) permission for a python preprocessor step.

    Declared in a skill's frontmatter `permissions.python: [{...}]` block.
    `safe` mode is AST-validated (import allowlist + banned builtin
    references, see _python_harness); `unsafe` requires --allow-unsafe-python
    at runtime AND startup-guard approval. `timeout` is wall-clock seconds;
    the parent SIGKILLs the child when it elapses.

    FP-0014 renamed `pure` → `safe` and `trusted` → `unsafe`. The legacy
    keywords are still accepted at parse time during the Track A → B
    transition (stdlib YAML lags); they are normalised to the new keywords
    here and rejected by the linter.
    """
    module: str
    function: str
    mode: str = "safe"   # "safe" | "unsafe"
    timeout: int = 30



@dataclass
class PermissionDecl:
    """Permissions declared in a skill's frontmatter `permissions:` block."""

    shell: bool = False
    mcp: list[str] = field(default_factory=list)
    tool: list[str] = field(default_factory=list)
    # Read-class ops outside CWD. Each entry: {"path": str, "scope": "just_path" | "recursive"}
    file_read: list[dict] = field(default_factory=list)
    # Write-class ops (write, edit, delete) outside the default zone.
    # Each entry: {"path": str, "scope": "just_path" | "recursive"}
    file_write: list[dict] = field(default_factory=list)
    # Python preprocessor steps the phase intends to run.
    python: list[PythonPermission] = field(default_factory=list)
    # PR37: per-agent MCP allowlist. None = no per-agent restriction (only
    # project-wide config applies). list[str] = agent must be in this list
    # AND the server must pass project-wide checks. "all" sentinel is
    # normalized to None by the loader before constructing PermissionDecl.
    allowed_mcp: list[str] | None = None
    # #571 collapse arc Phase 3: per-host HTTP allowlist for
    # ``reyn.safe.http.*`` calls from safe-mode python steps. Each
    # entry: {"host": str}. Empty list = no HTTP allowed via safe.http
    # (the ``web_fetch`` Tier-1 op route is unaffected — that's a
    # separate, LLM-callable surface with its own approval flow).
    http_get: list[dict] = field(default_factory=list)
    # #571 collapse arc Phase 3: per-key secret-store write allowlist
    # for ``~/.reyn/secrets.env`` writes. Each entry is a key name
    # (env var name).
    secret_write: list[str] = field(default_factory=list)
    # #571 collapse arc Phase 5 NOTE: the four former bool axes —
    # ``mcp_install`` / ``mcp_drop_server`` / ``cron_register`` /
    # ``index_drop`` — have been removed. Each was redundant with the
    # corresponding ``file.write`` (+ ``http.get`` for the registry
    # fetch in ``mcp_install``) declaration. Skills that previously
    # declared the bool axis migrate to the explicit list axes; see
    # the ``mcp_install`` stdlib skill for the canonical example. The
    # legacy ``PermissionDecl.from_dict`` keys (``mcp_install`` /
    # ``mcp_drop_server`` / ``cron_register`` / ``index_drop``) emit
    # ``DeprecationWarning`` when encountered so user-side skills can
    # be migrated; they no longer establish any runtime authority.

    @staticmethod
    def _parse_path_list(raw: object) -> list[dict]:
        if not raw:
            return []
        if not isinstance(raw, list):
            raw = [raw]
        out: list[dict] = []
        for item in raw:
            if isinstance(item, str):
                out.append({"path": item, "scope": "just_path"})
            elif isinstance(item, dict):
                out.append({
                    "path": str(item.get("path", "")),
                    "scope": str(item.get("scope", "just_path")),
                })
        return out

    @staticmethod
    def _parse_host_list(raw: object) -> list[dict]:
        """Parse a ``http.get`` list. Accepts ``[{host: str}]`` or ``[str]``.

        A bare string is normalised to ``{"host": <str>}``. Empty / non-list
        / non-dict / non-string entries are dropped silently — same lenient
        shape as ``_parse_path_list``.
        """
        if not raw:
            return []
        if not isinstance(raw, list):
            raw = [raw]
        out: list[dict] = []
        for item in raw:
            if isinstance(item, str):
                out.append({"host": item})
            elif isinstance(item, dict):
                host = str(item.get("host", ""))
                if host:
                    out.append({"host": host})
        return out

    @staticmethod
    def _parse_secret_key_list(raw: object) -> list[str]:
        """Parse a ``secret.write`` list of key names.

        Accepts ``list[str]`` or a bare ``str`` (normalised to a single-item
        list). Non-string entries are dropped silently.
        """
        if not raw:
            return []
        if not isinstance(raw, list):
            raw = [raw]
        return [str(item) for item in raw if isinstance(item, (str, int))]

    @staticmethod
    def _parse_python_list(raw: object) -> list[PythonPermission]:
        if not raw:
            return []
        if not isinstance(raw, list):
            raw = [raw]
        out: list[PythonPermission] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            module = str(item.get("module", ""))
            function = str(item.get("function", ""))
            if not module or not function:
                continue
            mode = str(item.get("mode", "safe"))
            if mode not in ("safe", "unsafe"):
                raise ValueError(
                    f"permissions.python: mode must be 'safe' or 'unsafe', got {mode!r}"
                )
            timeout = int(item.get("timeout", 30))
            out.append(PythonPermission(
                module=module, function=function, mode=mode, timeout=timeout,
            ))
        return out

    # #571 collapse arc Phase 5: legacy bool-axis keys carried for
    # deprecation-warning purposes only. The compat shim that previously
    # expanded these into ``file_write`` / ``http_get`` entries was
    # removed because the corresponding ``require_*`` methods no longer
    # exist — declaring a legacy bool axis no longer establishes any
    # runtime authority. Skills must migrate to the explicit list axes.
    _LEGACY_BOOL_AXIS_KEYS: ClassVar[tuple[str, ...]] = (
        "mcp_install",
        "mcp_drop_server",
        "cron_register",
        "index_drop",
    )

    @classmethod
    def from_dict(cls, d: dict | None) -> "PermissionDecl":
        if not d:
            return cls()
        # #571 collapse arc Phase 5: warn on legacy bool-axis keys so
        # user-side skills get a visible migration prompt. The values
        # themselves are no longer consulted — skills must declare the
        # equivalent file.write / http.get / secret.write entries
        # explicitly. See the ``mcp_install`` stdlib skill for the
        # canonical migration pattern.
        for legacy_key in cls._LEGACY_BOOL_AXIS_KEYS:
            if d.get(legacy_key):
                import warnings
                warnings.warn(
                    f"permissions.{legacy_key}: <bool> is removed in the "
                    f"#571 collapse arc (Phase 5). Replace it with the "
                    f"explicit list axes: file.write / http.get / secret.write. "
                    f"See docs/concepts/permission-model.md → Collapse arc.",
                    DeprecationWarning,
                    stacklevel=3,
                )
        return cls(
            shell=bool(d.get("shell", False)),
            mcp=_normalize_paths(d.get("mcp")),
            tool=_normalize_paths(d.get("tool")),
            file_read=cls._parse_path_list(d.get("file.read")),
            file_write=cls._parse_path_list(d.get("file.write")),
            python=cls._parse_python_list(d.get("python")),
            http_get=cls._parse_host_list(d.get("http.get")),
            secret_write=cls._parse_secret_key_list(d.get("secret.write")),
        )


class PermissionResolver:
    """
    Resolves permission requests against config, saved approvals, and a
    ``RequestBus`` for user prompts.

    The bus is supplied per-call (`require_*`, `startup_guard`, …) by the
    caller, since the bus is tied to the Agent that's running while the
    resolver is shared across runs in long-lived sessions (chat).
    """

    def __init__(
        self,
        config_permissions: dict,
        project_root: Path | None = None,
        interactive: bool = True,
        unsafe_python_allowed: bool = False,
        # FP-0014 compat: accept the legacy keyword name during the Track A → B
        # transition. New callers should use `unsafe_python_allowed`.
        trusted_python_allowed: bool | None = None,
    ) -> None:
        self._config = config_permissions or {}
        self._project_root = (project_root or Path.cwd()).resolve()
        self._interactive = interactive
        self._approvals_path = self._project_root / ".reyn" / "approvals.yaml"
        self._session: dict[str, bool] = {}
        self._saved: dict[str, bool] = self._load_saved()
        if trusted_python_allowed is not None:
            unsafe_python_allowed = trusted_python_allowed
        self._unsafe_python_allowed = unsafe_python_allowed
        # #398 v4 emitter wiring: subscribers fired when ``_persist`` lands
        # an approval (= "always allow") or revoke decision to approvals.yaml.
        # ChatSession registers a callback that mints a ``state_change``
        # history entry so the LLM sees "permission for X was
        # granted/revoked" in its next turn — directly mitigates the
        # #352 in-context-learning refusal trap. PermissionResolver is
        # shared across sessions; each ChatSession registers its own
        # callback so a project-wide grant notifies every active session.
        self._on_persist_callbacks: list[Callable[[str, bool], None]] = []

    # ── Public read helpers (= Tier-C1 cleanup wave 27) ───────────────────

    def saved_get(self, key: str) -> bool | None:
        """Read accessor for the persisted approvals map. Returns the
        stored boolean (or None when not yet recorded)."""
        return self._saved.get(key)

    def on_persist_callback_count(self) -> int:
        """Return the number of registered ``on_persist`` callbacks.

        Tests / observers use this to verify register / unregister
        balance without reaching into the internal list.
        """
        return len(self._on_persist_callbacks)

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_saved(self) -> dict[str, bool]:
        if not self._approvals_path.exists():
            return {}
        try:
            import yaml
            data = yaml.safe_load(self._approvals_path.read_text(encoding="utf-8")) or {}
            return {k: bool(v) for k, v in data.items() if isinstance(v, bool)}
        except Exception:
            return {}

    def _persist(self, key: str, approved: bool) -> None:
        self._saved[key] = approved
        self._session[key] = approved
        try:
            import yaml
            self._approvals_path.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if self._approvals_path.exists():
                existing = yaml.safe_load(
                    self._approvals_path.read_text(encoding="utf-8")
                ) or {}
            existing[key] = approved
            self._approvals_path.write_text(
                yaml.dump(existing, allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )
        except Exception:
            pass
        # #398 v4 emitter wiring: notify subscribers (= ChatSession
        # instances that registered themselves) so the LLM sees the
        # permission change as a ``state_change`` history entry next
        # turn. Iterate a snapshot so a callback that unregisters
        # itself mid-iteration doesn't trip the loop. Each callback is
        # wrapped in try/except — observability must not break the
        # core persistence path.
        for cb in list(self._on_persist_callbacks):
            try:
                cb(key, approved)
            except Exception:
                # Defensive: bad subscriber (= dead session reference,
                # callback bug) must not crash _persist.
                pass

    # ── #398 v4 emitter wiring (= state_change subscriber API) ──────────────

    def register_on_persist(
        self, callback: Callable[[str, bool], None],
    ) -> None:
        """Subscribe to ``_persist`` events for emitter wiring (= #398 v4).

        ``callback(key, approved)`` is invoked after the approval is
        written to ``approvals.yaml``. Used by ChatSession to mint
        a ``state_change`` history entry per ``notify_state_change``
        so the LLM sees the permission update in its next turn
        (= directly mitigates the #352 in-context-learning refusal
        trap pattern).

        Multiple ChatSessions can register the same shared resolver
        so a project-wide grant notifies every active session
        independently.
        """
        self._on_persist_callbacks.append(callback)

    def unregister_on_persist(
        self, callback: Callable[[str, bool], None],
    ) -> bool:
        """Detach a previously registered callback.

        Returns True iff the callback was found and removed. Use this
        on ChatSession shutdown to prevent dead-session callbacks from
        accumulating in long-running PermissionResolver instances
        (= the shared singleton model in ``reyn web`` / ``reyn run``
        sessions outlive individual ChatSessions).
        """
        try:
            self._on_persist_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    # ── Config check ─────────────────────────────────────────────────────────

    def _is_config_approved(self, key: str) -> bool:
        if self._config.get(key) == "allow":
            return True
        dot = key.find(".")
        if dot != -1:
            top, sub = key[:dot], key[dot + 1:]
            val = self._config.get(top)
            if val == "allow":
                return True
            if isinstance(val, dict) and val.get(sub) == "allow":
                return True
        return False

    def _is_config_denied(self, key: str) -> bool:
        """Return True when config explicitly sets `key` (or a parent key) to 'deny'."""
        if self._config.get(key) == "deny":
            return True
        dot = key.find(".")
        if dot != -1:
            top, sub = key[:dot], key[dot + 1:]
            val = self._config.get(top)
            if val == "deny":
                return True
            if isinstance(val, dict) and val.get(sub) == "deny":
                return True
        return False

    # ── Core approval (non-file ops) ──────────────────────────────────────────

    async def _approve(
        self,
        key: str,
        description: str,
        bus: RequestBus,
        *,
        user_prompt: str | None = None,
    ) -> bool:
        if self._is_config_approved(key):
            return True
        # Composite keys (e.g. "skill_router/python.safe/./mod.py:fn") accept
        # a kind-level blanket grant in config (e.g. "python.safe: allow").
        # The startup_guard already honors this; mirror the behavior at the
        # runtime check so config and startup are consistent.
        for part in key.split("/"):
            if "." in part and self._is_config_approved(part):
                return True
        if key in self._session:
            return self._session[key]
        if key in self._saved:
            v = self._saved[key]
            self._session[key] = v
            return v
        if not self._interactive:
            return False
        return await self._prompt(key, description, bus, user_prompt=user_prompt)

    async def _prompt(
        self,
        key: str,
        description: str,
        bus: RequestBus,
        *,
        user_prompt: str | None = None,
    ) -> bool:
        # Issue #224: when the caller passes a user-facing question
        # (e.g. "Allow fetching this URL?"), use it as the prompt header
        # so light-users see a natural-language ask instead of the
        # internal config key. Fallback "Permission request — {key}"
        # preserves backward-compat — no in-tree caller currently relies
        # on it; reserved for future test / external caller compat.
        iv = UserIntervention(
            kind="permission.generic",
            prompt=user_prompt or f"Permission request — {key}",
            detail=description or key,
            choices=generic_yn_choices(),
        )
        answer = await bus.request(iv)
        choice = answer.choice_id
        if choice == YES:
            self._session[key] = True
            return True
        if choice == ALWAYS:
            self._persist(key, True)
            return True
        if choice == NEVER:
            self._persist(key, False)
            return False
        # NO or unknown → deny (session-only)
        self._session[key] = False
        return False

    # ── File access approval (read + write) ───────────────────────────────────

    def _is_path_approved_for(self, path: str, skill_name: str, kind: str) -> bool:
        """Return True if path is covered by any saved/session approval for this skill+kind.

        kind is "file.read" or "file.write".
        """
        base = self._project_root
        p = Path(path).expanduser()
        p_resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
        prefix = f"{skill_name}/{kind}/"
        combined = {**self._saved, **self._session}
        for key, approved in combined.items():
            if not approved or not key.startswith(prefix):
                continue
            approved_str = key[len(prefix):]
            approved_p = _expand(approved_str.rstrip("/"))
            if approved_str.endswith("/"):
                try:
                    p_resolved.relative_to(approved_p)
                    return True
                except ValueError:
                    pass
            else:
                if p_resolved == approved_p:
                    return True
        return False

    # Backwards-compatible alias used by older write-class call sites.
    def _is_path_approved(self, path: str, skill_name: str) -> bool:
        return self._is_path_approved_for(path, skill_name, "file.write")

    def _is_host_approved_for(
        self, host: str, skill_name: str, kind: str = "http.get",
    ) -> bool:
        """Return True if ``host`` is covered by a saved/session approval.

        Hosts are exact-string-matched against the persisted approval
        key (= ``<skill>/http.get/<host>``). Mirrors
        :meth:`_is_path_approved_for` but skips the filesystem
        resolution because hosts are network identifiers, not paths.
        """
        if not skill_name or not host:
            return False
        key = f"{skill_name}/{kind}/{host}"
        return bool(self._saved.get(key) or self._session.get(key))

    def session_approve_path(
        self, path: str, skill_name: str, kind: str, recursive: bool = False,
    ) -> None:
        """Mark `path` as approved for this session only (not persisted).

        Used to suppress startup_guard prompts for paths a stdlib skill
        declares but the caller wants to silently approve up-front (avoids an
        interactive prompt before the chat REPL takes over stdin).

        kind: "file.read" or "file.write". When recursive=True the approval
        covers the directory and everything beneath it.
        """
        p = str(_expand(path))
        if recursive:
            p = p.rstrip("/") + "/"
        self._session[f"{skill_name}/{kind}/{p}"] = True

    def session_approve_host(
        self, host: str, skill_name: str, kind: str = "http.get",
    ) -> None:
        """Mark ``host`` as approved for this session only (not persisted).

        Sibling of :meth:`session_approve_path` for the ``http.get`` axis
        (#571 Phase 7). Hosts are network identifiers, not paths, so they
        do not go through ``_expand`` / filesystem resolution. Persistence
        key matches what :meth:`_is_host_approved_for` reads, so tests
        and operator-startup code can pre-seed approvals via this public
        surface instead of mutating ``_session`` directly.
        """
        if not skill_name or not host:
            return
        self._session[f"{skill_name}/{kind}/{host}"] = True

    async def _prompt_file_access(
        self, path: str, scope: str, skill_name: str, kind: str, bus: RequestBus,
    ) -> bool:
        """Prompt the user to approve a file access. Returns True if approved.

        kind is "file.read" or "file.write". scope is the declared scope from
        the phase's permissions block: "recursive" makes the [r] option grant
        access to everything under `path` itself; "just_path" (default) makes
        [r] grant the parent directory recursively.
        """
        if not self._interactive:
            return False
        verb = "Read" if kind == "file.read" else "Write"
        if scope == "recursive":
            recursive_target = str(Path(path).expanduser()).rstrip("/") + "/"
            recursive_label = path.rstrip("/") + "/"
        else:
            recursive_target = str(Path(path).expanduser().parent) + "/"
            recursive_label = recursive_target
        iv = UserIntervention(
            kind=f"permission.{kind}",
            prompt=f"{verb} access request: {path!r} [{scope}]",
            detail=f"recursive target would be {recursive_label!r}",
            choices=file_access_choices(recursive_label),
        )
        answer = await bus.request(iv)
        choice = answer.choice_id
        if choice == YES:
            self._session[f"{skill_name}/{kind}/{path}"] = True
            return True
        if choice == JUST_PATH:
            self._persist(f"{skill_name}/{kind}/{path}", True)
            return True
        if choice == RECURSIVE:
            self._persist(f"{skill_name}/{kind}/{recursive_target}", True)
            return True
        # NO or unknown → deny (session-only)
        self._session[f"{skill_name}/{kind}/{path}"] = False
        return False

    async def _prompt_file_write(
        self, path: str, scope: str, skill_name: str, bus: RequestBus,
    ) -> bool:
        return await self._prompt_file_access(path, scope, skill_name, "file.write", bus)

    async def startup_guard(
        self, skill: "Skill", skill_name: str, bus: "RequestBus | None",
    ) -> None:
        """
        Pre-flight permission check: scan all phase declarations, collect paths that
        fall outside the default zones, and ask the user to approve them before
        execution starts. Already-approved and config-approved paths are skipped.

        Non-interactive runs require approvals to be in place beforehand: either
        pre-approved in reyn.yaml / reyn.local.yaml (layer 3) or persisted to
        .reyn/approvals.yaml from a prior interactive run (layer 2).

        B49 W2-S5 fix (2026-05-22): ``bus`` may be ``None`` when the caller
        is a non-interactive context (e.g. a preprocessor sub-skill run
        invoked via ``run_skill`` op from inside ``iterate`` /
        ``run_op``). If all permissions are already approved or the skill
        declares none that need approval, the guard returns without using
        the bus. If unapproved permissions are found and ``bus is None``,
        a ``RuntimeError`` is raised with a clear message naming the
        pending permissions so the caller can surface the
        mis-configuration. Previously, ``run_orchestrator`` had an
        unconditional pre-check ``if intervention_bus is None: raise``
        that blocked any preprocessor sub-skill call when the resolver
        was configured, regardless of whether prompts were actually
        needed; that pre-check is removed and the None-handling moves
        here, where it can branch on actual prompt necessity.
        """
        write_requests: list[dict] = []
        read_requests: list[dict] = []
        write_seen: set[tuple] = set()
        read_seen: set[tuple] = set()

        decl = skill.permissions  # aggregated upper bound across all phases
        for entry in decl.file_write:
            path = entry.get("path", "")
            scope = entry.get("scope", "just_path")
            if not path:
                continue
            if _in_default_write_zone(path):
                continue
            if self._is_config_approved("file.write"):
                continue
            if self._is_path_approved_for(path, skill_name, "file.write"):
                continue
            key = (path, scope)
            if key not in write_seen:
                write_seen.add(key)
                write_requests.append({"path": path, "scope": scope})

        for entry in decl.file_read:
            path = entry.get("path", "")
            scope = entry.get("scope", "just_path")
            if not path:
                continue
            if _in_default_read_zone(path):
                continue
            if self._is_config_approved("file.read"):
                continue
            if self._is_path_approved_for(path, skill_name, "file.read"):
                continue
            key = (path, scope)
            if key not in read_seen:
                read_seen.add(key)
                read_requests.append({"path": path, "scope": scope})

        # Python preprocessor steps — both safe and unsafe require approval.
        # Unsafe additionally needs unsafe_python_allowed (checked here so the
        # user is told why their startup is being aborted before any prompts fire).
        python_requests: list[dict] = []
        python_seen: set[tuple] = set()
        for entry in decl.python:
            key = (entry.module, entry.function)
            if key in python_seen:
                continue
            python_seen.add(key)
            kind = "python.unsafe" if entry.mode == "unsafe" else "python.safe"
            if self._is_config_approved(kind):
                continue
            approval_key = f"{skill_name}/{kind}/{entry.module}:{entry.function}"
            if approval_key in self._saved or approval_key in self._session:
                continue
            python_requests.append({
                "module": entry.module, "function": entry.function,
                "mode": entry.mode,
            })

        # Hard-fail before prompting if an unsafe step appears without the flag.
        for req in python_requests:
            if req["mode"] == "unsafe" and not self._unsafe_python_allowed:
                raise PermissionError(
                    f"Skill '{skill_name}' declares an unsafe "
                    f"python step ({req['module']}:{req['function']}) but "
                    f"--allow-unsafe-python was not provided. Re-run with the flag "
                    f"to enable unsafe-mode Python preprocessor steps."
                )

        # #571 Phase 7: http.get specific declarations follow the
        # file.write pattern — startup_guard prompts the operator once
        # per skill+host and persists under ``<skill>/http.get/<host>``.
        # Wildcard ``"*"`` entries are skipped here; their prompt fires
        # JIT in ``require_http_get`` at the actual host gate.
        http_get_requests: list[dict] = []
        http_get_seen: set[str] = set()
        for entry in decl.http_get:
            if not isinstance(entry, dict):
                continue
            host = entry.get("host", "")
            if not host or host == "*":
                continue
            if host in http_get_seen:
                continue
            if self._is_config_approved("web.fetch"):
                continue
            if self._is_config_approved(f"http.get.{host}"):
                continue
            if self._is_host_approved_for(host, skill_name, "http.get"):
                continue
            http_get_seen.add(host)
            http_get_requests.append({"host": host})

        if not (write_requests or read_requests or python_requests or http_get_requests):
            return

        # B49 W2-S5 fix (2026-05-22): bus may be None in non-interactive
        # contexts (= preprocessor sub-skill run). If we reach here,
        # prompts are needed but no bus is available — surface a clear
        # error rather than silently skipping approvals or crashing
        # inside _prompt_*.
        if bus is None:
            pending = (
                [f"file.read:{r['path']}" for r in read_requests]
                + [f"file.write:{r['path']}" for r in write_requests]
                + [f"python:{r['module']}:{r['function']}" for r in python_requests]
                + [f"http.get:{r['host']}" for r in http_get_requests]
            )
            raise RuntimeError(
                f"Skill '{skill_name}' has unapproved permissions "
                f"({', '.join(pending)}) but no intervention_bus is "
                f"available to prompt the user. Pre-approve via "
                f"reyn.yaml or an interactive run before using this "
                f"skill non-interactively."
            )

        for req in read_requests:
            await self._prompt_file_access(
                req["path"], req["scope"], skill_name, "file.read", bus,
            )
        for req in write_requests:
            await self._prompt_file_access(
                req["path"], req["scope"], skill_name, "file.write", bus,
            )
        for req in python_requests:
            kind = "python.unsafe" if req["mode"] == "unsafe" else "python.safe"
            key = f"{skill_name}/{kind}/{req['module']}:{req['function']}"
            await self._prompt_python(key, req["module"], req["function"], req["mode"], bus)
        for req in http_get_requests:
            await self._prompt_http_get(req["host"], skill_name, bus)

    async def _prompt_http_get(
        self, host: str, skill_name: str, bus: RequestBus,
    ) -> bool:
        """Approve a specific http.get host at startup; persist on yes.

        #571 Phase 7: mirrors ``_prompt_file_access`` for HTTP host
        approvals. Uses the generic yes/no/always choice set (= host
        has no scope axis, so the file-access ``just_path`` /
        ``recursive`` choice doesn't apply). Persistence key is
        ``<skill>/http.get/<host>``.
        """
        if not self._interactive:
            self._session[f"{skill_name}/http.get/{host}"] = False
            return False
        iv = UserIntervention(
            kind="permission.http.get",
            prompt=f"Allow fetching from {host!r}?",
            detail=f"skill {skill_name!r} requests http.get for host {host!r}",
            choices=generic_yn_choices(),
        )
        answer = await bus.request(iv)
        choice = answer.choice_id
        key = f"{skill_name}/http.get/{host}"
        if choice == YES:
            self._session[key] = True
            return True
        if choice == ALWAYS:
            self._persist(key, True)
            return True
        # NO or unknown → deny (session-only)
        self._session[key] = False
        return False

    async def _prompt_python(
        self, key: str, module: str, function: str, mode: str, bus: RequestBus,
    ) -> bool:
        """Approve a python step; persist on yes."""
        if not self._interactive:
            # stdlib skills set unsafe_python_allowed=True — their python steps
            # are safe by construction and must auto-approve even in non-interactive
            # mode (--non-interactive).  User-supplied unsafe steps without the
            # flag are already hard-rejected in startup_guard before this point.
            #
            # Apply the auto-allow to BOTH unsafe and safe modes. `safe` is the
            # more-restricted capability (per _python_allowlist.py), so any
            # context that auto-allows unsafe MUST auto-allow safe — otherwise
            # stdlib `mode: safe` is strictly more locked-down than stdlib
            # `mode: unsafe`, which is semantically backwards. Pre-seeding the
            # session here also primes the matching require_python check at
            # runtime so the two code paths agree.
            if self._unsafe_python_allowed:
                self._session[key] = True
                return True
            self._session[key] = False
            return False
        verb = "UNSAFE" if mode == "unsafe" else "safe"
        iv = UserIntervention(
            kind="permission.python",
            prompt=f"Python step ({verb}): {module}:{function}",
            detail=f"approval key: {key}",
            choices=python_choices(),
        )
        answer = await bus.request(iv)
        choice = answer.choice_id
        if choice == YES:
            self._session[key] = True
            return True
        if choice == ALWAYS:
            self._persist(key, True)
            return True
        # NO or unknown → deny (session-only)
        self._session[key] = False
        return False

    # ── Public check methods ──────────────────────────────────────────────────

    def require_file_read(self, decl: PermissionDecl, path: str, skill_name: str = "") -> None:
        """
        Raise PermissionError if read/glob/grep access to path is not allowed.
        Default zone (CWD and below) is always granted.
        Outside CWD, the path must have been approved at startup or via config.

        FP-0008 PR-H (2026-05-28): in non-interactive mode where the
        startup guard cannot prompt the user, honor the skill's
        declared paths directly. This brings runtime into alignment
        with declared intent for batch/cron/CI workflows. Same class
        of wiring-gap fix as PR #1004 (= Tool→OpContext bridge).
        Interactive mode is unchanged — the user may decline at
        startup, decline is tracked in ``_session``, and is respected
        by ``_is_path_approved_for`` above.
        """
        if _in_default_read_zone(path):
            return
        if self._is_config_approved("file.read"):
            return
        if self._is_path_approved_for(path, skill_name, "file.read"):
            return
        if not self._interactive and _decl_covers_path(decl.file_read, path):
            return
        raise PermissionError(
            f"read from '{path}' was not approved. "
            f"Declare it in the skill.md frontmatter:\n"
            f"  permissions:\n"
            f"    file.read:\n"
            f"      - path: {path}\n"
            f"        scope: just_path\n"
            f"Then re-run — the startup guard will ask for approval before execution starts."
        )

    def require_file_write(self, decl: PermissionDecl, path: str, skill_name: str = "") -> None:
        """
        Raise PermissionError if write/edit/delete access to path is not allowed.
        Default zone (.reyn/, reyn/) is always granted.
        Outside the default zone, the path must have been approved at startup.

        FP-0008 PR-H (2026-05-28): in non-interactive mode where the
        startup guard cannot prompt the user, honor the skill's
        declared paths directly. See ``require_file_read`` for the
        rationale (= PR #1004 class N=2 trigger; wiring gap that
        leaves declared intent un-honored in batch mode).
        """
        if _in_default_write_zone(path):
            return
        if self._is_config_approved("file.write"):
            return
        if self._is_path_approved_for(path, skill_name, "file.write"):
            return
        if not self._interactive and _decl_covers_path(decl.file_write, path):
            return
        raise PermissionError(
            f"write to '{path}' was not approved. "
            f"Declare it in the skill.md frontmatter:\n"
            f"  permissions:\n"
            f"    file.write:\n"
            f"      - path: {path}\n"
            f"        scope: just_path\n"
            f"Then re-run — the startup guard will ask for approval before execution starts."
        )

    async def require_http_get(
        self,
        decl: PermissionDecl,
        host: str,
        bus: "RequestBus | None" = None,
        skill_name: str = "",
    ) -> None:
        """Gate HTTP access to ``host`` (#571 Phase 7 unification).

        Mirrors the ``file.write`` model — declaration is intent, the
        prompt fires at the timing where the host actually becomes
        known:

        - **Specific declared host** (``http.get: [{host: "api.github.com"}]``):
          ``startup_guard`` prompts the operator once per skill+host
          at startup and persists the decision to approvals.yaml under
          ``<skill>/http.get/<host>``. Runtime is then silent — this
          method finds the persisted approval and passes.
        - **Wildcard** (``http.get: [{host: "*"}]`` or ``["*"]``): host
          set is unknown at write-time (= LLM picks at runtime), so
          the prompt fires here at the actual host gate. Same
          ``<skill>/http.get/<host>`` persistence; ALWAYS / NEVER
          choices apply per-host.
        - **No declaration**: legacy ``web.fetch`` compat fallback
          (deprecation-warned). Will become a hard error in a future
          release.

        Backward-compat:

        - ``web.fetch: deny`` config overrides any wildcard permission.
        - ``web.fetch: allow`` config pre-approves any host without
          prompting (= equivalent to selecting ALWAYS for all hosts).
        - The legacy ``web.fetch`` session/saved approval still
          authorises any host while the deprecation period is active.

        ``bus`` is required when the wildcard path or the
        legacy-fallback path needs to prompt; sync contexts (=
        safe.http subprocess) must use specific declarations only.
        """
        # Config-tier deny always wins.
        if self._is_config_denied("web.fetch"):
            raise PermissionError(
                f"HTTP access to host {host!r} denied by config "
                f"(web.fetch: deny)."
            )
        if self._is_config_denied(f"http.get.{host}"):
            raise PermissionError(
                f"HTTP access to host {host!r} denied by config "
                f"(http.get.{host}: deny)."
            )

        # Config-tier allow short-circuits everything (= operator's
        # blanket pre-approval — present today as ``web.fetch: allow``).
        if self._is_config_approved("web.fetch"):
            return
        if self._is_config_approved(f"http.get.{host}"):
            return

        # Persisted per-host approval (= startup_guard for specific,
        # prior runtime decision for wildcard).
        if skill_name and self._is_host_approved_for(host, skill_name, "http.get"):
            return
        # Legacy session/saved ``web.fetch`` approval still authorises
        # every host while the deprecation window is open.
        if self._saved.get("web.fetch") or self._session.get("web.fetch"):
            return

        # Did the skill declare this host explicitly or via wildcard?
        has_specific = any(
            isinstance(e, dict) and e.get("host") == host for e in decl.http_get
        )
        has_wildcard = any(
            isinstance(e, dict) and e.get("host") == "*" for e in decl.http_get
        )

        if has_specific or has_wildcard:
            # Need to prompt — either startup_guard was skipped (=
            # non-interactive run with a still-unapproved specific decl)
            # or this is the wildcard JIT path.
            if bus is None:
                raise PermissionError(
                    f"HTTP access to host {host!r} requires an interactive "
                    f"prompt but no bus is available. Pre-approve via "
                    f"reyn.yaml (`permissions.web.fetch: allow` for blanket, "
                    f"or run interactively so startup_guard can collect "
                    f"approvals)."
                )
            approval_key = f"{skill_name}/http.get/{host}"
            label = (
                f"web fetch from host: {host!r}"
                if has_wildcard
                else f"http.get for host: {host!r}"
            )
            approved = await self._approve(
                approval_key, label, bus,
                user_prompt=f"Allow fetching from {host!r}?",
            )
            if not approved:
                raise PermissionError(
                    f"HTTP access to host {host!r} denied."
                )
            return

        # No declaration at all — legacy ``web_fetch`` compat path
        # for the segmented migration window. Skills that previously
        # relied on the Tier-1 default-allow behaviour still work
        # while we wait for them to declare ``http.get`` explicitly.
        import warnings
        warnings.warn(
            f"HTTP access to host {host!r} from skill {skill_name!r} "
            f"without an http.get declaration. This will become a hard "
            f"error in a future release. Add to skill.md:\n"
            f"  permissions:\n"
            f"    http.get:\n"
            f"      - host: '*'   # LLM-driven host selection\n"
            f"or list specific hosts.",
            DeprecationWarning,
            stacklevel=2,
        )
        if bus is None:
            raise PermissionError(
                f"HTTP access to host {host!r} not declared and no "
                f"interactive bus available for legacy compat prompt."
            )
        approved = await self._approve(
            "web.fetch",  # legacy key — shared across all hosts during the compat window
            f"web fetch from host: {host!r} (legacy compat)",
            bus,
            user_prompt=f"Allow fetching from {host!r}?",
        )
        if not approved:
            raise PermissionError(
                f"HTTP access to host {host!r} denied (legacy compat path)."
            )

    def require_secret_write(
        self, decl: PermissionDecl, key: str, skill_name: str = "",
    ) -> None:
        """Raise PermissionError if secret-store write of ``key`` is not declared.

        Two declaration shapes are accepted:

        - **Specific key** — ``secret.write: ["GITHUB_TOKEN"]`` authorises
          only that exact key. Use when the skill knows at write-time
          which env-var names it will save.
        - **Wildcard** ``"*"`` — ``secret.write: ["*"]`` authorises any
          key. Use when the key set is determined at runtime from
          external metadata (= ``mcp_install``'s ``isSecret``
          environment variables from the registry response). The
          security gate in this case is the operator's per-value prompt
          at op-execution time; the wildcard declaration is the
          author's acknowledgement that the skill will route through
          that prompt-then-save flow.

        Specific entries take precedence — a skill that lists both
        ``"GITHUB_TOKEN"`` and ``"*"`` is functionally equivalent to
        just ``"*"`` but conveys intent more clearly.
        """
        if key in decl.secret_write or "*" in decl.secret_write:
            return
        raise PermissionError(
            f"Secret-store write of key {key!r} not declared in skill permissions. "
            f"Add to skill.md frontmatter:\n"
            f"  permissions:\n"
            f"    secret.write:\n"
            f"      - {key}\n"
            f"or use the wildcard form for runtime-determined keys:\n"
            f"  permissions:\n"
            f"    secret.write:\n"
            f"      - '*'\n"
        )

    def is_read_allowed(self, path: str, skill_name: str = "") -> bool:
        """Check if reading `path` is allowed.

        Allowed if: the path is in the default read zone (under CWD), OR config
        grants `file.read: allow`, OR a per-skill approval covers it.
        """
        if _in_default_read_zone(path):
            return True
        if self._is_config_approved("file.read"):
            return True
        if skill_name and self._is_path_approved_for(path, skill_name, "file.read"):
            return True
        return False

    def is_write_allowed(self, path: str, skill_name: str = "") -> bool:
        """Check if writing `path` is allowed.

        Allowed if: default write zone, OR config grants `file.write: allow`, OR
        a per-skill approval covers it.
        """
        if _in_default_write_zone(path):
            return True
        if self._is_config_approved("file.write"):
            return True
        if skill_name and self._is_path_approved_for(path, skill_name, "file.write"):
            return True
        return False

    async def require_shell(
        self, decl: PermissionDecl, cmd: str, bus: "RequestBus | None",
    ) -> None:
        if not decl.shell:
            raise PermissionError(
                f"shell access not declared in skill permissions. "
                f"Add `permissions:\\n  shell: true` to the skill.md frontmatter."
                f" (cmd: {cmd!r})"
            )
        if not await self._approve(
            "shell",
            f"shell command: {cmd!r}",
            bus,
            user_prompt="Allow running this shell command?",
        ):
            raise PermissionError(f"shell access denied (cmd: {cmd!r})")

    async def require_mcp(
        self, decl: PermissionDecl, server: str, bus: RequestBus,
    ) -> None:
        # PR37: per-agent allowlist check (narrower than project config).
        # None means no per-agent restriction; list means server must be in it.
        if decl.allowed_mcp is not None and server not in decl.allowed_mcp:
            raise PermissionError(
                f"MCP server {server!r} not in allowed_mcp for caller "
                f"(agent allowlist exhausted)"
            )
        if server not in decl.mcp:
            raise PermissionError(
                f"MCP server {server!r} not declared in skill permissions. "
                f"Add `permissions:\\n  mcp: [{server}]` to the skill.md frontmatter."
            )
        if not await self._approve(
            f"mcp.{server}",
            f"MCP server: {server!r}",
            bus,
            user_prompt=f"Allow access to MCP server {server!r}?",
        ):
            raise PermissionError(f"MCP server {server!r} access denied")

    async def require_python(
        self, decl: PermissionDecl, module: str, function: str,
        bus: "RequestBus | None",
        skill_name: str = "",
    ) -> PythonPermission:
        """Resolve which python permission entry applies; raise if denied.

        Lookup is by (module, function). Safe-mode steps need a one-time
        startup_guard approval (saved per skill+module:function). Unsafe-mode
        steps additionally require unsafe_python_allowed=True (set by the
        --allow-unsafe-python CLI flag).

        B49 W2-S5 fix (2026-05-22): ``bus`` may be ``None`` in
        non-interactive contexts (= preprocessor / postprocessor python
        step invoked from a sub-skill run). ``_approve`` short-circuits
        before reaching the prompt path when the permission is
        config-approved or saved/session-approved, so the bus is only
        consulted when an interactive prompt is genuinely required.
        With ``self._interactive=False`` and no prior approval,
        ``_approve`` returns False without touching the bus, and this
        method raises PermissionError as before.
        """
        matching = [
            p for p in decl.python
            if p.module == module and p.function == function
        ]
        if not matching:
            raise PermissionError(
                f"python step {module}:{function} is not declared in skill permissions. "
                f"Add to the skill.md frontmatter:\n"
                f"  permissions:\n"
                f"    python:\n"
                f"      - module: {module}\n"
                f"        function: {function}\n"
                f"        mode: safe"
            )
        perm = matching[0]
        if perm.mode == "unsafe":
            if not self._unsafe_python_allowed:
                raise PermissionError(
                    f"python step {module}:{function} declares mode='unsafe' "
                    f"but --allow-unsafe-python was not provided. "
                    f"Unsafe python runs unrestricted user code; pass the flag "
                    f"only when you trust the skill source."
                )
            key = f"{skill_name}/python.unsafe/{module}:{function}"
            # Non-interactive + unsafe_python_allowed=True (stdlib skills) must
            # auto-approve without a prompt.  Pre-seed the session key so _approve()
            # returns True instead of auto-denying the non-interactive branch.
            if not self._interactive and self._unsafe_python_allowed:
                self._session.setdefault(key, True)
            if not await self._approve(
                key,
                f"unsafe python step: {module}:{function}",
                bus,
                user_prompt=f"Run unsafe python {module}.{function}?",
            ):
                raise PermissionError(
                    f"unsafe python step {module}:{function} denied by user"
                )
            return perm
        # safe mode
        key = f"{skill_name}/python.safe/{module}:{function}"
        # Mirror the unsafe-mode stdlib auto-allow path. Safe mode is more
        # restricted (per _python_allowlist.py), so any context that auto-allows
        # unsafe MUST auto-allow safe. Without this, stdlib skills declaring
        # mode: safe fail in non-interactive `reyn run` while their unsafe
        # siblings succeed — semantically backwards.
        if not self._interactive and self._unsafe_python_allowed:
            self._session.setdefault(key, True)
        if not await self._approve(
            key,
            f"safe python step: {module}:{function}",
            bus,
            user_prompt=f"Run python {module}.{function}?",
        ):
            raise PermissionError(
                f"safe python step {module}:{function} denied by user"
            )
        return perm

    async def require_tool(
        self, decl: PermissionDecl, tool: str, bus: RequestBus,
    ) -> None:
        if tool not in decl.tool:
            raise PermissionError(
                f"tool {tool!r} not declared in skill permissions. "
                f"Add `permissions:\\n  tool: [{tool}]` to the skill.md frontmatter."
            )
        if not await self._approve(
            f"tool.{tool}",
            f"tool: {tool!r}",
            bus,
            user_prompt=f"Allow tool {tool!r}?",
        ):
            raise PermissionError(f"tool {tool!r} access denied")

    async def require_media_load(
        self,
        *,
        size_bytes: int,
        source: str,
        mime_type: str,
        max_bytes: int,
        on_oversize: str,
        bus: RequestBus,
    ) -> None:
        """Multi-modal cluster gate (issue #364) — applies to binary media
        (images today; audio/video deferred) about to be loaded into LLM
        context from web__fetch / file__read / MCP / user input.

        Under-limit: returns immediately (= zero overhead for the common
        case). At-or-over limit: behaves per ``on_oversize`` (see
        ``MultimodalConfig``):

          - ``allow`` → pass.
          - ``deny`` → ``PermissionError`` (caller emits status="denied").
          - ``ask`` → interactive prompt via 4-layer ``_approve`` flow:

            Layer 1 (config):    ``media.oversize: allow`` → pre-approve.
                                 ``media.oversize: deny`` → deny.
            Layer 2 (approvals): ``media.oversize`` persistent decision.
            Layer 3 (session):   prior in-memory decision (= ALWAYS/NEVER).
            Layer 4 (interactive): prompt with concrete size + source.

        The shared infrastructure is reused by #365 (file__read binary) and
        #366 (user chat input image) — only the ``source`` string differs.
        """
        if size_bytes <= max_bytes:
            return
        if on_oversize == "allow":
            return
        if on_oversize == "deny":
            raise PermissionError(
                f"media load denied: {source} returned {size_bytes} bytes "
                f"(limit {max_bytes}, multimodal.on_oversize=deny)"
            )
        # on_oversize == "ask" → 4-layer approval path.
        size_mb = size_bytes / 1_000_000
        limit_mb = max_bytes / 1_000_000
        description = (
            f"{source} returned media ({mime_type}, {size_mb:.1f}MB). "
            f"Limit is {limit_mb:.1f}MB."
        )
        if not await self._approve(
            "media.oversize",
            description,
            bus,
            user_prompt="Load this oversize media into context?",
        ):
            raise PermissionError(
                f"media load denied by user: {source} ({size_bytes} bytes "
                f"> {max_bytes})"
            )

    async def require_web_fetch(self, url: str, bus: RequestBus) -> None:
        """Tier 1 gate for web_fetch — no declaration required, full 4-layer approval.

        FP-0022: web_fetch was previously gated only by catalog-level config
        (web.fetch: allow); without that, the LLM never saw the tool. Now uses
        the standard _approve() flow (config / approvals.yaml / session / interactive).

        Resolution order:
          Layer 1a: ``web.fetch: deny`` in reyn.yaml → immediate PermissionError.
          Layer 1b: ``web.fetch: allow`` in reyn.yaml → pre-approved, no prompt.
          Layer 2:  approvals.yaml persistent decision.
          Layer 3:  in-memory session decision.
          Layer 4:  interactive prompt (YES/NO/ALWAYS/NEVER).

        ``web.fetch: allow`` existing config entries continue to work unchanged —
        _is_config_approved() handles them at Layer 1b.
        """
        if self._is_config_denied("web.fetch"):
            raise PermissionError(
                "web fetch denied by config (web.fetch: deny)"
            )
        if not await self._approve(
            "web.fetch",
            f"web fetch: {url}",
            bus,
            user_prompt="Allow fetching this URL?",
        ):
            raise PermissionError("web fetch denied")
