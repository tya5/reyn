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

Config pre-approval (reyn.yaml / .reyn/config.yaml):
  permissions:
    shell: allow
    file.write: allow   # grants all write-class ops for all skills
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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
    InterventionBus,
    UserIntervention,
)

if TYPE_CHECKING:
    from .models import Skill


_DEFAULT_WRITE_ZONES = (".reyn", "reyn")


def _normalize_paths(v: object) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _expand(path_str: str) -> Path:
    """Expand ~ and resolve a path string to an absolute Path."""
    return Path(path_str).expanduser().resolve()


def _in_default_write_zone(path_str: str) -> bool:
    """Return True if path falls within a default-granted write zone (.reyn/ or reyn/)."""
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


@dataclass
class PythonPermission:
    """Per-(module, function) permission for a python preprocessor step.

    Declared in a skill's frontmatter `permissions.python: [{...}]` block.
    `pure` mode is sandboxed (AST + restricted builtins, see _python_harness);
    `trusted` requires --allow-untrusted-python at runtime AND startup-guard
    approval. `timeout` is wall-clock seconds; the parent SIGKILLs the child
    when it elapses.
    """
    module: str
    function: str
    mode: str = "pure"   # "pure" | "trusted"
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
            mode = str(item.get("mode", "pure"))
            if mode not in ("pure", "trusted"):
                raise ValueError(
                    f"permissions.python: mode must be 'pure' or 'trusted', got {mode!r}"
                )
            timeout = int(item.get("timeout", 30))
            out.append(PythonPermission(
                module=module, function=function, mode=mode, timeout=timeout,
            ))
        return out

    @classmethod
    def from_dict(cls, d: dict | None) -> "PermissionDecl":
        if not d:
            return cls()
        return cls(
            shell=bool(d.get("shell", False)),
            mcp=_normalize_paths(d.get("mcp")),
            tool=_normalize_paths(d.get("tool")),
            file_read=cls._parse_path_list(d.get("file.read")),
            file_write=cls._parse_path_list(d.get("file.write")),
            python=cls._parse_python_list(d.get("python")),
        )


class PermissionResolver:
    """
    Resolves permission requests against config, saved approvals, and an
    `InterventionBus` for user prompts.

    The bus is supplied per-call (`require_*`, `startup_guard`, …) by the
    caller, since the bus is tied to the Agent that's running while the
    resolver is shared across runs in long-lived sessions (chat).
    """

    def __init__(
        self,
        config_permissions: dict,
        project_root: Path | None = None,
        interactive: bool = True,
        trusted_python_allowed: bool = False,
    ) -> None:
        self._config = config_permissions or {}
        self._project_root = (project_root or Path.cwd()).resolve()
        self._interactive = interactive
        self._approvals_path = self._project_root / ".reyn" / "approvals.yaml"
        self._session: dict[str, bool] = {}
        self._saved: dict[str, bool] = self._load_saved()
        self._trusted_python_allowed = trusted_python_allowed

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

    # ── Core approval (non-file ops) ──────────────────────────────────────────

    async def _approve(self, key: str, description: str, bus: InterventionBus) -> bool:
        if self._is_config_approved(key):
            return True
        # Composite keys (e.g. "skill_router/python.pure/./mod.py:fn") accept
        # a kind-level blanket grant in config (e.g. "python.pure: allow").
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
        return await self._prompt(key, description, bus)

    async def _prompt(self, key: str, description: str, bus: InterventionBus) -> bool:
        iv = UserIntervention(
            kind="permission.generic",
            prompt=f"Permission request — {key}",
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

    async def _prompt_file_access(
        self, path: str, scope: str, skill_name: str, kind: str, bus: InterventionBus,
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
        self, path: str, scope: str, skill_name: str, bus: InterventionBus,
    ) -> bool:
        return await self._prompt_file_access(path, scope, skill_name, "file.write", bus)

    async def startup_guard(
        self, skill: "Skill", skill_name: str, bus: InterventionBus,
    ) -> None:
        """
        Pre-flight permission check: scan all phase declarations, collect paths that
        fall outside the default zones, and ask the user to approve them before
        execution starts. Already-approved and config-approved paths are skipped.

        Non-interactive runs require approvals to be in place beforehand: either
        pre-approved in reyn.yaml / reyn.local.yaml (layer 3) or persisted to
        .reyn/approvals.yaml from a prior interactive run (layer 2).
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

        # Python preprocessor steps — both pure and trusted require approval.
        # Trusted additionally needs trusted_python_allowed (checked here so the
        # user is told why their startup is being aborted before any prompts fire).
        python_requests: list[dict] = []
        python_seen: set[tuple] = set()
        for entry in decl.python:
            key = (entry.module, entry.function)
            if key in python_seen:
                continue
            python_seen.add(key)
            kind = "python.trusted" if entry.mode == "trusted" else "python.pure"
            if self._is_config_approved(kind):
                continue
            approval_key = f"{skill_name}/{kind}/{entry.module}:{entry.function}"
            if approval_key in self._saved or approval_key in self._session:
                continue
            python_requests.append({
                "module": entry.module, "function": entry.function,
                "mode": entry.mode,
            })

        # Hard-fail before prompting if a trusted step appears without the flag.
        for req in python_requests:
            if req["mode"] == "trusted" and not self._trusted_python_allowed:
                raise PermissionError(
                    f"Skill '{skill_name}' declares a trusted "
                    f"python step ({req['module']}:{req['function']}) but "
                    f"--allow-untrusted-python was not provided. Re-run with the flag "
                    f"to enable trusted-mode Python preprocessor steps."
                )

        if not (write_requests or read_requests or python_requests):
            return

        for req in read_requests:
            await self._prompt_file_access(
                req["path"], req["scope"], skill_name, "file.read", bus,
            )
        for req in write_requests:
            await self._prompt_file_access(
                req["path"], req["scope"], skill_name, "file.write", bus,
            )
        for req in python_requests:
            kind = "python.trusted" if req["mode"] == "trusted" else "python.pure"
            key = f"{skill_name}/{kind}/{req['module']}:{req['function']}"
            await self._prompt_python(key, req["module"], req["function"], req["mode"], bus)

    async def _prompt_python(
        self, key: str, module: str, function: str, mode: str, bus: InterventionBus,
    ) -> bool:
        """Approve a python step; persist on yes."""
        if not self._interactive:
            self._session[key] = False
            return False
        verb = "TRUSTED" if mode == "trusted" else "pure"
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
        """
        if _in_default_read_zone(path):
            return
        if self._is_config_approved("file.read"):
            return
        if self._is_path_approved_for(path, skill_name, "file.read"):
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
        """
        if _in_default_write_zone(path):
            return
        if self._is_config_approved("file.write"):
            return
        if self._is_path_approved_for(path, skill_name, "file.write"):
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
        self, decl: PermissionDecl, cmd: str, bus: InterventionBus,
    ) -> None:
        if not decl.shell:
            raise PermissionError(
                f"shell access not declared in skill permissions. "
                f"Add `permissions:\\n  shell: true` to the skill.md frontmatter."
                f" (cmd: {cmd!r})"
            )
        if not await self._approve("shell", f"shell command: {cmd!r}", bus):
            raise PermissionError(f"shell access denied (cmd: {cmd!r})")

    async def require_mcp(
        self, decl: PermissionDecl, server: str, bus: InterventionBus,
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
        if not await self._approve(f"mcp.{server}", f"MCP server: {server!r}", bus):
            raise PermissionError(f"MCP server {server!r} access denied")

    async def require_python(
        self, decl: PermissionDecl, module: str, function: str,
        bus: InterventionBus,
        skill_name: str = "",
    ) -> PythonPermission:
        """Resolve which python permission entry applies; raise if denied.

        Lookup is by (module, function). Pure-mode steps need a one-time
        startup_guard approval (saved per skill+module:function). Trusted-mode
        steps additionally require trusted_python_allowed=True (set by the
        --allow-untrusted-python CLI flag).
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
                f"        mode: pure"
            )
        perm = matching[0]
        if perm.mode == "trusted":
            if not self._trusted_python_allowed:
                raise PermissionError(
                    f"python step {module}:{function} declares mode='trusted' "
                    f"but --allow-untrusted-python was not provided. "
                    f"Trusted python runs unrestricted user code; pass the flag "
                    f"only when you trust the skill source."
                )
            key = f"{skill_name}/python.trusted/{module}:{function}"
            if not await self._approve(key, f"trusted python step: {module}:{function}", bus):
                raise PermissionError(
                    f"trusted python step {module}:{function} denied by user"
                )
            return perm
        # pure mode
        key = f"{skill_name}/python.pure/{module}:{function}"
        if not await self._approve(key, f"pure python step: {module}:{function}", bus):
            raise PermissionError(
                f"pure python step {module}:{function} denied by user"
            )
        return perm

    async def require_tool(
        self, decl: PermissionDecl, tool: str, bus: InterventionBus,
    ) -> None:
        if tool not in decl.tool:
            raise PermissionError(
                f"tool {tool!r} not declared in skill permissions. "
                f"Add `permissions:\\n  tool: [{tool}]` to the skill.md frontmatter."
            )
        if not await self._approve(f"tool.{tool}", f"tool: {tool!r}", bus):
            raise PermissionError(f"tool {tool!r} access denied")
