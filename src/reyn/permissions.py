"""
Phase-level permission declarations and approval resolution.

Default grants (no declaration needed):
  file read/glob/grep  — any path within the project root (CWD)
  file write/edit/delete — under project/.reyn/ or project/reyn/ only

Outside the defaults → the phase must declare the path AND the user must approve:
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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Skill


_DEFAULT_WRITE_ZONES = (".reyn", "reyn")


def _normalize_paths(v: object) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _in_default_write_zone(path_str: str) -> bool:
    """Return True if path falls within a default-granted write zone (.reyn/ or reyn/)."""
    base = Path.cwd()
    p = Path(path_str)
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    for zone in _DEFAULT_WRITE_ZONES:
        try:
            resolved.relative_to((base / zone).resolve())
            return True
        except ValueError:
            pass
    return False


@dataclass
class PermissionDecl:
    """Permissions declared in a phase's frontmatter `permissions:` block."""

    shell: bool = False
    mcp: list[str] = field(default_factory=list)
    tool: list[str] = field(default_factory=list)
    # Write-class ops (write, edit, delete) outside the default zone.
    # Each entry: {"path": str, "scope": "just_path" | "recursive"}
    file_write: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PermissionDecl":
        if not d:
            return cls()
        fw_raw = d.get("file.write") or []
        if not isinstance(fw_raw, list):
            fw_raw = [fw_raw]
        file_write: list[dict] = []
        for item in fw_raw:
            if isinstance(item, str):
                file_write.append({"path": item, "scope": "just_path"})
            elif isinstance(item, dict):
                file_write.append({
                    "path": str(item.get("path", "")),
                    "scope": str(item.get("scope", "just_path")),
                })
        return cls(
            shell=bool(d.get("shell", False)),
            mcp=_normalize_paths(d.get("mcp")),
            tool=_normalize_paths(d.get("tool")),
            file_write=file_write,
        )


_PROMPT_TEMPLATE = (
    "\n  Permission request — {perm}\n"
    "  {description}\n"
    "  Allow? [y]es / [n]o / [A]lways / [N]ever: "
)


class PermissionResolver:
    """
    Resolves permission requests against config, saved approvals, and interactive prompts.

    Thread this through OSRuntime → ControlIRExecutor → execute().
    """

    def __init__(
        self,
        config_permissions: dict,
        project_root: Path | None = None,
        interactive: bool = True,
    ) -> None:
        self._config = config_permissions or {}
        self._project_root = (project_root or Path.cwd()).resolve()
        self._interactive = interactive
        self._approvals_path = self._project_root / ".reyn" / "approvals.yaml"
        self._session: dict[str, bool] = {}
        self._saved: dict[str, bool] = self._load_saved()

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

    def _approve(self, key: str, description: str) -> bool:
        if self._is_config_approved(key):
            return True
        if key in self._session:
            return self._session[key]
        if key in self._saved:
            v = self._saved[key]
            self._session[key] = v
            return v
        if not self._interactive:
            return False
        return self._prompt(key, description)

    def _prompt(self, key: str, description: str) -> bool:
        prompt = _PROMPT_TEMPLATE.format(perm=key, description=description or key)
        try:
            ans = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans in ("y", "Y", "yes"):
            self._session[key] = True
            return True
        if ans == "A":
            self._persist(key, True)
            return True
        if ans == "N":
            self._persist(key, False)
            return False
        self._session[key] = False
        return False

    # ── File write approval ───────────────────────────────────────────────────

    def _is_path_approved(self, path: str, skill_name: str) -> bool:
        """Return True if path is covered by any saved or session approval for this skill."""
        base = self._project_root
        p = Path(path)
        p_resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
        prefix = f"{skill_name}/file.write/"
        combined = {**self._saved, **self._session}
        for key, approved in combined.items():
            if not approved or not key.startswith(prefix):
                continue
            approved_str = key[len(prefix):]
            approved_p = (base / approved_str).resolve()
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

    def _prompt_file_write(self, path: str, scope: str, skill_name: str) -> bool:
        """Prompt the user to approve a write-class file access. Returns True if approved."""
        parent = str(Path(path).parent) + "/"
        prompt = (
            f"  Write access: {path!r}  [{scope}]\n"
            f"  [y]es (this run) / [j]ust this path always / "
            f"[r]ecursive from {parent!r} always / [N]o: "
        )
        if not self._interactive:
            return False
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans == "y":
            self._session[f"{skill_name}/file.write/{path}"] = True
            return True
        if ans == "j":
            self._persist(f"{skill_name}/file.write/{path}", True)
            return True
        if ans == "r":
            self._persist(f"{skill_name}/file.write/{parent}", True)
            return True
        self._session[f"{skill_name}/file.write/{path}"] = False
        return False

    def startup_guard(self, skill: "Skill", skill_name: str) -> None:
        """
        Pre-flight permission check: scan all phase declarations, collect paths that
        fall outside the default write zone, and ask the user to approve them before
        execution starts. Already-approved and config-approved paths are skipped.
        """
        requests: list[dict] = []
        seen: set[tuple] = set()

        for phase_name, phase in skill.phases.items():
            for entry in phase.permissions.file_write:
                path = entry.get("path", "")
                scope = entry.get("scope", "just_path")
                if not path:
                    continue
                if _in_default_write_zone(path):
                    continue
                if self._is_config_approved("file.write"):
                    continue
                if self._is_path_approved(path, skill_name):
                    continue
                key = (path, scope)
                if key not in seen:
                    seen.add(key)
                    requests.append({"path": path, "scope": scope, "phase": phase_name})

        if not requests:
            return

        print(f"\n  Skill '{skill_name}' requests write access outside the default zone:")
        for req in requests:
            print(f"    • {req['path']}  [{req['scope']}]  (phase: {req['phase']})")
        print()
        for req in requests:
            self._prompt_file_write(req["path"], req["scope"], skill_name)

    # ── Public check methods ──────────────────────────────────────────────────

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
        if self._is_path_approved(path, skill_name):
            return
        raise PermissionError(
            f"write to '{path}' was not approved. "
            f"Declare it in the phase frontmatter:\n"
            f"  permissions:\n"
            f"    file.write:\n"
            f"      - path: {path}\n"
            f"        scope: just_path\n"
            f"Then re-run — the startup guard will ask for approval before execution starts."
        )

    def require_shell(self, decl: PermissionDecl, cmd: str = "") -> None:
        if not decl.shell:
            raise PermissionError(
                f"shell access not declared in phase permissions. "
                f"Add `permissions:\\n  shell: true` to the phase frontmatter."
                f" (cmd: {cmd!r})"
            )
        if not self._approve("shell", f"shell command: {cmd!r}"):
            raise PermissionError(f"shell access denied (cmd: {cmd!r})")

    def require_mcp(self, decl: PermissionDecl, server: str) -> None:
        if server not in decl.mcp:
            raise PermissionError(
                f"MCP server {server!r} not declared in phase permissions. "
                f"Add `permissions:\\n  mcp: [{server}]` to the phase frontmatter."
            )
        if not self._approve(f"mcp.{server}", f"MCP server: {server!r}"):
            raise PermissionError(f"MCP server {server!r} access denied")

    def require_tool(self, decl: PermissionDecl, tool: str) -> None:
        if tool not in decl.tool:
            raise PermissionError(
                f"tool {tool!r} not declared in phase permissions. "
                f"Add `permissions:\\n  tool: [{tool}]` to the phase frontmatter."
            )
        if not self._approve(f"tool.{tool}", f"tool: {tool!r}"):
            raise PermissionError(f"tool {tool!r} access denied")
