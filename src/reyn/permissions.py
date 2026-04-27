"""
Phase-level permission declarations and approval resolution.

Default grants (always allowed, no declaration needed):
  file.read  — any workspace-relative path
  file.write — any workspace-relative path (workspace enforces containment)

Everything else requires a phase declaration AND approval:
  shell      — declare permissions.shell: true
  mcp        — declare permissions.mcp: [server_name, ...]
  tool       — declare permissions.tool: [tool_name, ...]
  file.delete  — declare permissions.file.delete: [glob, ...]
  file.move    — declare permissions.file.move: {from: ..., to: ...}
  file.read    — absolute-path reads require explicit declaration

Approval hierarchy (first match wins):
  1. Config pre-approval  (reyn.yaml or .reyn/config.yaml: permissions.shell: allow)
  2. Saved approval       (.reyn/approvals.yaml — persisted "Always" choices)
  3. Session approval     (in-memory "Always" from this run)
  4. Interactive prompt   ([y]es / [n]o / [A]lways / [N]ever)
  5. Deny
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _normalize_paths(v: object) -> list[str]:
    """Accept str, list[str], or None; return list[str]."""
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


@dataclass
class PermissionDecl:
    """Permissions declared in a phase's frontmatter `permissions:` block."""

    shell: bool = False
    mcp: list[str] = field(default_factory=list)
    tool: list[str] = field(default_factory=list)
    file_read: list[str] = field(default_factory=list)    # extra absolute paths beyond defaults
    file_write: list[str] = field(default_factory=list)   # reserved for future use
    file_delete: list[str] = field(default_factory=list)
    file_move_from: list[str] = field(default_factory=list)
    file_move_to: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PermissionDecl":
        if not d:
            return cls()
        move_raw = d.get("file.move") or {}
        if isinstance(move_raw, dict):
            move_from = _normalize_paths(move_raw.get("from"))
            move_to = _normalize_paths(move_raw.get("to"))
        else:
            move_from = _normalize_paths(move_raw)
            move_to = []
        return cls(
            shell=bool(d.get("shell", False)),
            mcp=_normalize_paths(d.get("mcp")),
            tool=_normalize_paths(d.get("tool")),
            file_read=_normalize_paths(d.get("file.read")),
            file_write=_normalize_paths(d.get("file.write")),
            file_delete=_normalize_paths(d.get("file.delete")),
            file_move_from=move_from,
            file_move_to=move_to,
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
        """
        Check if key is pre-approved in config.

        Key formats: "shell", "file.delete", "mcp.github", "tool.mytool"
        Config format:
          permissions:
            shell: allow
            mcp:
              github: allow
            file.delete: allow
        """
        # Exact key match: "shell: allow", "file.delete: allow"
        if self._config.get(key) == "allow":
            return True
        # "mcp.github" → config["mcp"]["github"] == "allow"
        dot = key.find(".")
        if dot != -1:
            top, sub = key[:dot], key[dot + 1:]
            val = self._config.get(top)
            if val == "allow":
                return True
            if isinstance(val, dict) and val.get(sub) == "allow":
                return True
        return False

    # ── Core approval ─────────────────────────────────────────────────────────

    def _approve(self, key: str, description: str) -> bool:
        """Return True if the operation is approved; False to deny."""
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
        # "n" or anything else → deny for this session only
        self._session[key] = False
        return False

    # ── Public check methods ──────────────────────────────────────────────────

    def require_shell(self, decl: PermissionDecl, cmd: str = "") -> None:
        """Raise PermissionError if shell access is not allowed."""
        if not decl.shell:
            raise PermissionError(
                f"shell access not declared in phase permissions. "
                f"Add `permissions:\\n  shell: true` to the phase frontmatter."
                f" (cmd: {cmd!r})"
            )
        if not self._approve("shell", f"shell command: {cmd!r}"):
            raise PermissionError(f"shell access denied (cmd: {cmd!r})")

    def require_mcp(self, decl: PermissionDecl, server: str) -> None:
        """Raise PermissionError if MCP server access is not allowed."""
        if server not in decl.mcp:
            raise PermissionError(
                f"MCP server {server!r} not declared in phase permissions. "
                f"Add `permissions:\\n  mcp: [{server}]` to the phase frontmatter."
            )
        if not self._approve(f"mcp.{server}", f"MCP server: {server!r}"):
            raise PermissionError(f"MCP server {server!r} access denied")

    def require_tool(self, decl: PermissionDecl, tool: str) -> None:
        """Raise PermissionError if tool access is not allowed."""
        if tool not in decl.tool:
            raise PermissionError(
                f"tool {tool!r} not declared in phase permissions. "
                f"Add `permissions:\\n  tool: [{tool}]` to the phase frontmatter."
            )
        if not self._approve(f"tool.{tool}", f"tool: {tool!r}"):
            raise PermissionError(f"tool {tool!r} access denied")
