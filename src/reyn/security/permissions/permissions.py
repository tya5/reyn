"""
Actor-level permission declarations and approval resolution.

Default grants (no declaration needed):
  file read/glob/grep  — any path within the project root (CWD)
  file write/edit/delete — under project/.reyn/ only

Outside the defaults → the actor must declare the path AND the user must approve:
  file.read:  [{path: <path>, scope: just_path|recursive}]   (paths outside CWD)
  file.write: [{path: <path>, scope: just_path|recursive}]
  mcp        — declare permissions.mcp: [server_name, ...]
  tool       — declare permissions.tool: [tool_name, ...]

Approval choices (shown once at startup before execution starts):
  [y]es                        — allow for this run only
  [j]ust this path always      — persist approval for this exact path + actor
  [r]ecursive from parent      — persist approval for the parent directory + actor (covers all files under it)
  [N]o                         — deny

Approval keys are actor-scoped to prevent external-actor privilege escalation:
  "{actor}/file.write/{path}"   (just_path)
  "{actor}/file.write/{dir}/"   (recursive, trailing slash signals recursive)

Config pre-approval (reyn.yaml / reyn.local.yaml):
  permissions:
    file.write: allow   # grants all write-class ops for all actors
"""
from __future__ import annotations

import logging
import os
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
)
from reyn.security.permissions.approval_ledger import (
    RELATIVE_PATH as _APPROVAL_LEDGER_RELATIVE_PATH,
)
from reyn.user_intervention import (
    RequestBus,
    UserIntervention,
)

if TYPE_CHECKING:
    from reyn.security.permissions.file_scope import FileScopes
    from reyn.security.sandbox.policy import SandboxPolicy

logger = logging.getLogger(__name__)


_DEFAULT_WRITE_ZONES = (".reyn",)

# #571 collapse arc Phase 2 / realignment: canonical paths whose write
# must NOT be silently default-zone-allowed because a direct write would
# bypass an authorization / audit surface.
#
# Protect-at-use migration: ``.reyn/mcp.yaml`` and ``.reyn/cron.yaml`` were
# REMOVED from this set. Their config carve-out is redundant given a
# downstream use-gate — writing the config alone grants nothing usable:
#   - mcp.yaml: USING a server passes a per-server gate at call time
#     (``require_mcp``, op_runtime/mcp.py). Installing (= writing mcp.yaml)
#     without that gate is inert.
#   - cron.yaml: registering a job goes through the standard tool gate
#     (``require_cron_register`` / ``require_file_write``); fired jobs only
#     run under a user-launched in-process scheduler and their ops are
#     themselves permission-gated.
# ``.reyn/index/sources.yaml`` stays carved out transitionally until the
# index write-gate is effective end-to-end (#1320: the postprocessor scope
# must carry a sandbox-policy source; the S3.4 part1 op-layer gate alone
# does not fire in the real index flow). ``.reyn/approvals.yaml`` stays
# permanently — it is the approval store itself and has no downstream
# use-gate (see below).
#
# Agents that legitimately need to mutate a carved-out path must declare
# it explicitly via ``file.write: [{path: ...}]`` (or the bool-axis compat
# shim that auto-expands into the equivalent entry). Letting a safe-mode
# python step write one via the broad ``.reyn/`` default zone bypasses the
# corresponding gate. The narrow exception preserves the broad ``.reyn/``
# write zone for everything else (= chunkers, cursors, scratch state).
# #2248 PR-C: recovery-core write-gate prefixes. A file.write under these prefixes is
# NOT silently allowed by the broad ``.reyn/`` default zone — it must go through a
# dedicated op that emits a WAL event (mcp_install/drop, cron_register, index_drop write
# their `config/*.yaml` via an EXPLICIT file.write declaration; WAL/snapshot/tasks under
# `state/` use their own durable paths, never a raw file.write). A generic PREFIX rule
# (P7-clean — "deny raw file.write under the recovery-core prefixes", not a filename
# list): the directory boundary IS the write-gate boundary. ``config/index/sources.yaml``
# (formerly an explicit entry) is now covered by the ``config/`` prefix.
_RECOVERY_CORE_WRITE_PREFIXES = (
    ".reyn/config/",
    ".reyn/state/",
)

_CANONICAL_PROTECTED_WRITE_PATHS = (
    # #1199 security fix: the persisted approval store. It is written ONLY via
    # the gated approval-decision mechanism (``_persist`` — which also emits the
    # state_change audit signal). It is TOP-LEVEL (persist, #2248 A2-cont — not under
    # the config/ recovery-core prefix), so it needs this explicit carve-out: without
    # it a safe-mode file.write could inject an approval directly, bypassing the
    # user-approval gate + the audit, and (since approvals load once into
    # ``self._saved`` at startup) silently activating a never-approved grant on the
    # NEXT run = approval-audit bypass.
    #
    # #5153/#5170 moved the LIVE store from this snapshot to the append-only
    # ``.reyn/approvals.jsonl`` ledger (``ApprovalLedger`` — see
    # ``approval_ledger.py``) — #5173 found the carve-out was never updated to
    # follow: a safe-mode write could append a fake ``{"kind": "approval", ...,
    # "approved": true}`` record directly, the EXACT SAME bypass class #1199
    # closed, reopened against the new file. ``approvals.yaml`` itself stays
    # listed too — it is still read (one-time migration source,
    # ``migrate_legacy_snapshot``) and a legacy tree may still carry it.
    #
    # The jsonl entry is the IMPORTED constant, not a re-typed literal (#5173's
    # own root cause: a hand-typed copy of the live path is exactly what fell
    # silently out of sync with it last time). Renaming the ledger means
    # changing ``approval_ledger.RELATIVE_PATH`` once; this entry — and
    # ``self._approval_ledger_path`` below — follow automatically.
    ".reyn/approvals.yaml",
    _APPROVAL_LEDGER_RELATIVE_PATH,
)


def _normalize_paths(v: object) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _is_canonical_protected_write(path_str: str, base: "Path | None" = None) -> bool:
    """Return True if ``path_str`` resolves to one of the #571 protected paths.

    #1316: ``base`` is the project root the default zones are anchored to.
    ``None`` → ``Path.cwd()`` (the historical default; callers that set a
    project_root ≠ cwd must pass it so the zone base matches approvals)."""
    base = base or Path.cwd()
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    for rel in _CANONICAL_PROTECTED_WRITE_PATHS:
        if resolved == (base / rel).resolve():
            return True
    return False


def _is_under_recovery_core_prefix(path_str: str, base: "Path | None" = None) -> bool:
    """#2248 PR-C: True if ``path_str`` is under a recovery-core write-gate prefix
    (``.reyn/config/`` or ``.reyn/state/``). Such a write must NOT be silently allowed by
    the broad ``.reyn/`` default zone — it goes through a dedicated WAL-emitting op (which
    declares the path explicitly) rather than a raw ``file.write``."""
    base = base or Path.cwd()
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    for prefix in _RECOVERY_CORE_WRITE_PREFIXES:
        try:
            resolved.relative_to((base / prefix).resolve())
            return True
        except ValueError:
            pass
    return False


def _in_default_write_zone(path_str: str, base: "Path | None" = None) -> bool:
    """Return True if path falls within a default-granted write zone (.reyn/).

    Exception: canonical protected paths (see
    ``_CANONICAL_PROTECTED_WRITE_PATHS`` — ``.reyn/index/sources.yaml``
    transitionally + ``.reyn/approvals.yaml``) return False here so the
    write is forced through its explicit ``file.write`` declaration / gated
    flow rather than the broad ``.reyn/`` zone. ``.reyn/mcp.yaml`` and
    ``.reyn/cron.yaml`` were removed from this set (protect-at-use — their
    downstream use-gate makes the config carve-out redundant).
    """
    base = base or Path.cwd()
    if _is_canonical_protected_write(path_str, base) or _is_under_recovery_core_prefix(
        path_str, base,
    ):
        return False
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    for zone in _DEFAULT_WRITE_ZONES:
        try:
            resolved.relative_to((base / zone).resolve())
            return True
        except ValueError:
            pass
    return False


def _in_default_read_zone(path_str: str, base: "Path | None" = None) -> bool:
    """Return True if path falls within the default-granted read zone (the project
    root). #1316: ``base`` defaults to ``Path.cwd()`` (historical) — pass the
    resolver's ``_project_root`` so the read zone matches the approvals base."""
    base = base or Path.cwd()
    p = Path(path_str).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        return False


# #1199 S3.1c-1: _decl_covers_path was removed — the non-interactive decl
# auto-grant it served (FP-0008 PR-H) is gone (file gates are decl-less:
# zone OR approved). swe_bench now uses an explicit config-grant.


@dataclass
class PermissionDecl:
    """Permissions declared for an actor (via reyn.yaml `permissions:` or programmatically)."""

    mcp: list[str] = field(default_factory=list)
    tool: list[str] = field(default_factory=list)
    # Read-class ops outside CWD. Each entry: {"path": str, "scope": "just_path" | "recursive"}
    file_read: list[dict] = field(default_factory=list)
    # Write-class ops (write, edit, delete) outside the default zone.
    # Each entry: {"path": str, "scope": "just_path" | "recursive"}
    file_write: list[dict] = field(default_factory=list)
    # PR37: per-agent MCP allowlist. None = no per-agent restriction (only
    # project-wide config applies). list[str] = agent must be in this list
    # AND the server must pass project-wide checks. "all" sentinel is
    # normalized to None by the loader before constructing PermissionDecl.
    allowed_mcp: list[str] | None = None
    # #571 collapse arc Phase 3: per-host HTTP allowlist for
    # ``reyn.api.safe.http.*`` calls from safe-mode python steps. Each
    # entry: {"host": str}. Empty list = no HTTP allowed via safe.http
    # (the ``web_fetch`` Tier-1 op route is unaffected — that's a
    # separate, LLM-callable surface with its own approval flow).
    http_get: list[dict] = field(default_factory=list)
    # #571 collapse arc Phase 3: per-key secret-store write allowlist
    # for ``~/.reyn/secrets.env`` writes. Each entry is a key name
    # (env var name).
    secret_write: list[str] = field(default_factory=list)
    # #3198: per-name allowlist for ``${env:VAR}`` skill-load expansion
    # (``reyn.plugins.skill_load``) — the READ-side counterpart of
    # ``secret_write``'s WRITE-side allowlist, same shape (a specific name,
    # or the ``"*"`` wildcard). Deny-by-default: an empty/unset list means
    # NO ``${env:VAR}`` token expands (the token is left in the output
    # unexpanded, never blanked) — installing a plugin/skill does not by
    # itself grant it the process's environment. Declared via
    # ``permissions: env.expand: [NAME, ...]`` in reyn.yaml.
    env_expand: list[str] = field(default_factory=list)
    # #3901 PR-B ①: the actor's OWN declared intent for the two axes
    # permission previously left unconstrained (CapabilityAxis.SUBPROCESS /
    # ENV — AgentLayer.allows() used to fall through to True for both,
    # deferring entirely to SandboxLayer). Per the owner's split (#3901):
    # permission is what the OPERATOR RECOGNIZES and can therefore express as
    # "let this agent do X" — subprocess launch and which env-var NAMES pass
    # through to one are both things an operator names when granting an
    # agent capability, distinct from sandbox's job of bounding what the
    # agent does NOT get told about. Compat default (True) per owner ruling
    # #3202/#3901 (agent-decl axes default to what the launching shell could
    # already do; sandbox's SEPARATE deny-list axes are what narrows this).
    subprocess: bool = True
    # DISTINCT from ``env_expand`` above (that axis gates a SKILL.md body
    # reading a name FROM os.environ into the LLM's context; this axis is
    # the actor's OWN declared list of env-var names it is permitted to
    # pass through to a subprocess it launches — same vocabulary,
    # deliberately different capability, same non-conflation rule
    # ``env_expand``'s own comment states). Empty = "declares no names" —
    # per-value ∩ with SandboxPolicy's own (also empty-default, #3901 PR-B
    # ④) env_deny_names: an operator narrows by adding to EITHER list, an
    # actor's declaration alone does not widen past sandbox's own deny.
    env: list[str] = field(default_factory=list)
    # #571 collapse arc Phase 5 NOTE: the four former bool axes —
    # ``mcp_install`` / ``mcp_drop_server`` / ``cron_register`` /
    # ``index_drop`` — have been removed. Each was redundant with the
    # corresponding ``file.write`` (+ ``http.get`` for the registry
    # fetch in ``mcp_install``) declaration. Configs that previously
    # declared the bool axis migrate to the explicit list axes.
    # The legacy ``PermissionDecl.from_dict`` keys (``mcp_install`` /
    # ``mcp_drop_server`` / ``cron_register`` / ``index_drop``) emit
    # ``DeprecationWarning`` when encountered so existing configs can
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

    # #571 collapse arc Phase 5: legacy bool-axis keys carried for
    # deprecation-warning purposes only. The compat shim that previously
    # expanded these into ``file_write`` / ``http_get`` entries was
    # removed because the corresponding ``require_*`` methods no longer
    # exist — declaring a legacy bool axis no longer establishes any
    # runtime authority. Configs must migrate to the explicit list axes.
    _LEGACY_BOOL_AXIS_KEYS: ClassVar[tuple[str, ...]] = (
        "mcp_install",
        "mcp_drop_server",
        "cron_register",
        "index_drop",
    )

    @classmethod
    def from_dict(cls, d: dict | None) -> "PermissionDecl":
        # Fail-secure on a missing OR malformed (non-dict) permissions block. A
        # reyn.yaml ``permissions:`` block with a non-dict value (an authoring typo) is
        # not coerced by the loader and reaches here unguarded — ``d.get(...)`` on a
        # str/list would then crash with an unclear AttributeError. Default to an
        # EMPTY decl (no grants): crash-safe AND the secure default for a
        # permissions primitive.
        if not isinstance(d, dict):
            return cls()
        # Unsafe python step removed (tech debt). A skill / pipeline that still
        # declares ``mode: unsafe`` under ``permissions.python`` is rejected at
        # load — NEVER silently downgraded to safe (a silent downgrade would run
        # code the author believed was unsandboxed → confusing failures). Python
        # steps are now ALWAYS sandboxed (AST-allowlisted + restricted builtins).
        python_entries = d.get("python")
        if isinstance(python_entries, list):
            for entry in python_entries:
                if isinstance(entry, dict) and entry.get("mode") == "unsafe":
                    fn = entry.get("function") or "<unnamed>"
                    raise ValueError(
                        f"permissions.python: mode: unsafe was removed; python "
                        f"steps are always sandboxed (offending function: {fn!r}). "
                        f"Delete the 'mode: unsafe' line — safe mode is the only "
                        f"behaviour. If the step needs raw I/O, use the "
                        f"reyn.api.safe.* surface (permission-gated) or split the "
                        f"I/O out via a run_op."
                    )
        # #571 collapse arc Phase 5: warn on legacy bool-axis keys so
        # existing configs get a visible migration prompt. The values
        # themselves are no longer consulted — actors must declare the
        # equivalent file.write / http.get / secret.write entries
        # explicitly.
        for legacy_key in cls._LEGACY_BOOL_AXIS_KEYS:
            if d.get(legacy_key):
                import warnings
                warnings.warn(
                    f"permissions.{legacy_key}: <bool> is removed in the "
                    f"#571 collapse arc (Phase 5). Replace it with the "
                    f"explicit list axes: file.write / http.get / secret.write. "
                    f"See docs/concepts/runtime/permission-model.md → Collapse arc.",
                    DeprecationWarning,
                    stacklevel=3,
                )
        return cls(
            mcp=_normalize_paths(d.get("mcp")),
            tool=_normalize_paths(d.get("tool")),
            file_read=cls._parse_path_list(d.get("file.read")),
            file_write=cls._parse_path_list(d.get("file.write")),
            # "python" key grants no runtime authority — PYTHON axis removed
            # (zero live enforcement). It is only inspected above to fail-closed
            # on a removed ``mode: unsafe`` declaration.
            http_get=cls._parse_host_list(d.get("http.get")),
            secret_write=cls._parse_secret_key_list(d.get("secret.write")),
            env_expand=cls._parse_secret_key_list(d.get("env.expand")),
            # #3901 PR-B ①: compat default (True) when the key is omitted —
            # ``d.get("subprocess", True)`` mirrors omitted-key-keeps-the-
            # default everywhere else in this method (mcp/tool/etc. default
            # to their field's own empty-list default via _normalize_paths
            # on None). An explicit ``subprocess: false`` denies.
            subprocess=bool(d.get("subprocess", True)),
            env=cls._parse_secret_key_list(d.get("env")),
        )


def env_expand_allowed(decl: PermissionDecl, name: str) -> bool:
    """True iff *name* may be ``${env:name}``-expanded (#3198) — a specific
    declared name OR the ``"*"`` wildcard, faithful to ``secret_write``'s
    shape. Deny-by-default: an empty/unset ``decl.env_expand`` permits
    nothing.

    A free function (not a ``PermissionResolver`` method) because the
    decision needs only the static ``PermissionDecl`` — no approvals-file
    I/O, no sandbox/profile layer applies to this axis (see
    ``effective.SandboxLayer``/``ProfileLayer``, both ⊤ for
    ``ENV_EXPAND``) — so a caller with only a decl in hand (e.g.
    ``reyn.plugins.skill_load``, which must not construct a throwaway
    ``PermissionResolver`` per skill-body read) can call this directly.
    Routed through the SAME unified ``EffectivePermission`` model as every
    other axis so there is exactly one implementation of the membership
    rule (``PermissionResolver.is_env_expand_allowed`` delegates here).
    """
    from reyn.security.permissions.effective import (
        AgentLayer,
        CapabilityAxis,
        EffectivePermission,
    )

    return EffectivePermission([AgentLayer(decl)]).allows(
        CapabilityAxis.ENV_EXPAND, name,
    )


class PermissionResolver:
    """
    Resolves permission requests against config, saved approvals, and a
    ``RequestBus`` for user prompts.

    The bus is supplied per-call (`require_*`) by the
    caller, since the bus is tied to the Agent that's running while the
    resolver is shared across runs in long-lived sessions (chat).
    """

    def __init__(
        self,
        config_permissions: dict,
        project_root: Path | None = None,
        interactive: bool = True,
        # #1414: the default file read/write ZONE anchor. Distinct from
        # ``project_root`` (= the host-side approvals/config base). Under a
        # container backend the agent's file ops target the in-container repo
        # (base_dir=/testbed, #1410/#1411), so the zone must anchor there while
        # approvals.yaml stays host-side. ``None`` → defaults to ``project_root``
        # so host / interactive behaviour is byte-identical.
        file_zone_root: Path | None = None,
    ) -> None:
        self._config = config_permissions or {}
        self._project_root = (project_root or Path.cwd()).resolve()
        # #1414: zone anchor (container repo root under a container backend);
        # falls back to the host project_root (host-default byte-identical).
        self._file_zone_root = (
            Path(file_zone_root).resolve() if file_zone_root else self._project_root
        )
        self._interactive = interactive
        # #5153: the LEGACY snapshot path — read-only from here on (a
        # one-time migration source into the ledger, never written by
        # this class again; see ``_ensure_folded``/``migrate_legacy_snapshot``).
        self._approvals_path = self._project_root / ".reyn" / "approvals.yaml"
        # #5153: the append-only decision ledger that REPLACES the above
        # snapshot's read-modify-write. See ``approval_ledger.py``'s own
        # module docstring for the full rationale (3 independent writers
        # — this class, the CLI `permissions` command, the web router —
        # each needing momentary ownership of the WHOLE snapshot file to
        # change even one key was the root cause of a lost approval under
        # concurrent access).
        self._approval_ledger_path = self._project_root / Path(_APPROVAL_LEDGER_RELATIVE_PATH)
        self._session: dict[str, bool] = {}
        # #3671 P4 item D-1: lazy — disk read + YAML parse deferred to the
        # `_saved` property's first access, not paid on every PermissionResolver
        # construction (one is built per `reyn chat` startup, cost scales with
        # the number of persisted approval entries). Every existing internal
        # read (`self._saved.get(...)` / `self._saved[key] = ...` / etc.) goes
        # through the SAME property, so there is exactly one load site — no
        # caller can forget to trigger it, and mutation still works: the
        # property returns the SAME dict object each time, so in-place
        # `self._saved[key] = approved` still mutates the real stored dict.
        self.__saved: "dict[str, bool] | None" = None
        # #5042: PATH-flavor approvals only ("what dir/file was this grant
        # actually FOR, the moment it was last found valid") — a SIBLING
        # structure under its own top-level ``_bound_identities`` key in
        # the SAME approvals.yaml, never mixed into the approval rows
        # themselves. Kept separate rather than changing the approval
        # VALUE's own shape (bare `true`/`false`, unchanged) for two
        # reasons: (a) every OTHER approval flavor (host/plugin — #5042's
        # own architect ruling: "flavor を跨がない") never touches this at
        # all, by construction, since only _is_path_approved_for (the
        # file.read/file.write path) ever reads or writes it; (b) the
        # audit row itself — the thing #5042's own issue thread named as
        # "must not disappear" — stays BYTE-IDENTICAL to every approvals.
        # yaml written before this change, so a pre-existing file needs no
        # migration step: an entry with no sibling binding here is simply
        # unbound (acceptance ⑤), whether it predates #5042 entirely or is
        # a fresh grant that has not been used yet (acceptance ④).
        self.__bound_identities: "dict[str, tuple[int, float | None]] | None" = None
        # #5152 (architect ruling, issuecomment-5383544769): a session-life
        # counter of "identity comparison could not be confirmed" events —
        # see ``_identity_check_passes``'s own docstring. Exists so the
        # fail-closed default this PR adds is OBSERVABLE (count/log), not a
        # silent behaviour change; read via ``unconfirmable_identity_check_count``.
        self._unconfirmable_identity_checks: int = 0
        # #5152 (architect ruling, issuecomment-5383604927): one open fd
        # per bound key -- see ``_acquire_identity_fd``'s own docstring
        # for why this is what makes an ino comparison trustworthy
        # without ``st_birthtime``. #5157 (e2e-coder's TESTS-READY(B)
        # finding on #5152, architect ruling issuecomment-5383671820):
        # the population here is RATE-LIMITED BY A HUMAN, not machine-
        # driven growth -- one fd per distinct path-flavor approval, and
        # an approval is only ever created by a person granting it.
        # Architect explicitly rejected an LRU cap (would routinely
        # demote a still-protected key into the "cannot confirm" bucket,
        # making pool SIZE decide the security guarantee's scope). The
        # actual gap this PR closes: a same-key rebind was the ONLY
        # release path -- see ``_persist``'s own revoke branch, which now
        # also releases the fd (the same #5146-style "settle on
        # disappearance" idiom, this time for approval revocation).
        # ``bound_fd_count`` is the public read this issue's test-review
        # Q5 asked for.
        self._bound_fds: "dict[str, int]" = {}
        # #1383 (D12): scoped read-grants for OS-offloaded artifacts. When the OS
        # offloads an artifact to a state-dir path and hands the agent an
        # `artifact_ref` / `_offload_ref` pointing there, that path is outside the
        # default read zone (CWD) — the agent would be told to read a path it is
        # then denied. The offload-emit registers the EXACT path here (not the
        # whole state-dir → least-privilege); the read gate consults it. Resolved
        # absolute paths only; exact-match (no prefix grant).
        self._offload_read_paths: set[str] = set()
        # #398 v4 emitter wiring: subscribers fired when ``_persist`` lands
        # an approval (= "always allow") or revoke decision to approvals.yaml.
        # Session registers a callback that mints a ``state_change``
        # history entry so the LLM sees "permission for X was
        # granted/revoked" in its next turn — directly mitigates the
        # #352 in-context-learning refusal trap. PermissionResolver is
        # shared across sessions; each Session registers its own
        # callback so a project-wide grant notifies every active session.
        self._on_persist_callbacks: list[Callable[[str, bool], None]] = []

    # ── Public read helpers (= Tier-C1 cleanup wave 27) ───────────────────

    # ── The file path-set source (#3458) ─────────────────────────────────

    def file_scopes(self) -> "FileScopes":
        """The resolved ``permissions.file.read`` / ``file.write`` path sets.

        #3458: the ONE answer to "which paths are readable / writable". The
        runtime gates (:meth:`require_file_read` / :meth:`require_file_write` /
        :meth:`is_read_allowed` / :meth:`is_write_allowed`) and the
        advertisement side (the router tool catalog + the system prompt's
        ``## Files`` section) both obtain the set from here, which is a thin
        wrapper over :func:`reyn.security.permissions.file_scope.resolve_file_scopes`
        — a subsystem that has no resolver can call that function directly with
        ``(config, zone_root)`` and get the identical answer.
        """
        from reyn.security.permissions.file_scope import resolve_file_scopes

        return resolve_file_scopes(self._config, zone_root=self._file_zone_root)

    def advertised_file_permissions(self) -> dict | None:
        """``{"read": [paths], "write": [paths]}`` for the router tool catalog
        and system prompt, or ``None`` when BOTH axes are empty.

        #3458: derived from :meth:`file_scopes`, so what the model is told it
        may touch is the same set the gate enforces. ``None`` (= advertise no
        file tools) now means what it says — both axes resolve to the empty
        set (``deny`` / an explicit ``[]``) — instead of the pre-#3458 "the
        operator wrote nothing", which hid a live default-zone capability."""
        scopes = self.file_scopes()
        if scopes.read.is_empty and scopes.write.is_empty:
            return None
        return scopes.advertised()

    @property
    def project_root(self) -> Path:
        """The host-side approvals/config base (where ``.reyn`` lives). Public
        read accessor — callers (e.g. #1827 S4b's ``_untrusted`` profile load)
        resolve project paths from here instead of reaching into the private
        ``_project_root``."""
        return self._project_root

    def saved_get(self, key: str) -> bool | None:
        """Read accessor for the persisted approvals map. Returns the
        stored boolean (or None when not yet recorded)."""
        return self._saved.get(key)

    def bound_identity_get(self, key: str) -> "tuple[int, float | None] | None":
        """#5042: read accessor for the PATH-flavor identity binding,
        mirroring :meth:`saved_get`'s own convention — ``None`` when *key*
        has never been bound (never used, a legacy pre-#5042 entry, or a
        non-path-flavor key, which is never bound at all)."""
        return self._bound_identities.get(key)

    def unconfirmable_identity_check_count(self) -> int:
        """#5152 (architect ruling, issuecomment-5383604927): how many
        times an identity comparison could not be confirmed either way
        this session (no fd held for the key yet, and ``st_birthtime``
        unavailable) — the grant is HONORED, never denied, but counted
        here so that fact is observable rather than silent. See
        :meth:`_identity_check_passes`'s own docstring."""
        return self._unconfirmable_identity_checks

    def bound_fd_count(self) -> int:
        """#5157 (test-review Q5 — "what does this accumulate, and who
        bounds it?"): how many identity fds this process currently
        holds — one per distinct PATH-flavor approval a human has
        granted and used at least once (architect ruling,
        issuecomment-5383671820: rate-limited by human approval, not an
        LRU cap — released on a rebind or on that approval's own
        revocation, see :meth:`_release_identity_fd`). Public so the
        real in-use count is observable without reaching into private
        state."""
        return len(self._bound_fds)

    @property
    def approval_ledger_path(self) -> Path:
        """#5173: the resolved path this resolver's :class:`ApprovalLedger`
        reads/writes — public so a caller (or a test asserting the write-gate
        carve-out actually covers the LIVE file, not a hand-picked string
        that could silently drift from it) can read it without reaching into
        ``self._approval_ledger_path`` directly."""
        return self._approval_ledger_path

    def on_persist_callback_count(self) -> int:
        """Return the number of registered ``on_persist`` callbacks.

        Tests / observers use this to verify register / unregister
        balance without reaching into the internal list.
        """
        return len(self._on_persist_callbacks)

    # ── Persistence ──────────────────────────────────────────────────────────

    @property
    def _saved(self) -> dict[str, bool]:
        """#3671 P4 item D-1: the single owner of the lazy load — folds
        the #5153 append-only ``approvals.jsonl`` ledger (migrating a
        legacy ``approvals.yaml`` snapshot first, if present) on first
        access only, cached for the life of this resolver. Returns the
        SAME dict object across calls, so `self._saved[key] = value`
        (used by `_persist`) mutates the real cached dict, not a
        throwaway copy.

        #3671 P4 D-1 review (lead-coder): this IS a check-then-set
        (`self.__saved is None` → `self.__saved = ...`), the same SHAPE as
        the 6 races fixed in #3674 — but here it is safe WITHOUT a lock or
        an ownership/Future pattern, and that is a claim this comment must
        justify, not merely assert (#3674's own standard). One
        `PermissionResolver` IS shared across multiple `Session`s (PR10) —
        so this property IS reachable from more than one concurrently-
        running coroutine. What makes it safe regardless: `_ensure_folded()`
        contains NO `await` — the whole check-then-set body runs to
        completion inside a single asyncio task's turn with no yield point
        in between, so no other coroutine can observe `self.__saved` in a
        partially-updated state or race the assignment (asyncio's
        single-threaded cooperative scheduling — NOT a general
        thread-safety claim; if `PermissionResolver` were ever reached from
        a real OS thread — e.g. via `run_in_executor` — this reasoning would
        no longer hold and this property would need the same ownership
        treatment #3674 gave `ensure_litellm_ready`)."""
        if self.__saved is None:
            self._ensure_folded()
        assert self.__saved is not None  # _ensure_folded always sets both
        return self.__saved

    # ── #5042/#5153: bound identity for PATH-flavor approvals only ──────────

    @property
    def _bound_identities(self) -> "dict[str, tuple[int, float | None]]":
        """Same lazy-load-once shape as :attr:`_saved` — folded from the
        SAME ledger in the SAME pass (see :meth:`_ensure_folded`); mutation
        through the returned dict object persists via :meth:`_bind_identity`."""
        if self.__bound_identities is None:
            self._ensure_folded()
        assert self.__bound_identities is not None  # _ensure_folded always sets both
        return self.__bound_identities

    def _ensure_folded(self) -> None:
        """#5153: the single fold site both :attr:`_saved` and
        :attr:`_bound_identities` lazy-load through — one ledger read
        producing BOTH maps together (they were always derived from the
        same records; loading them separately would just be the same
        file parsed twice). Migrates a legacy ``approvals.yaml`` snapshot
        into the ledger first if the ledger doesn't exist yet (see
        :func:`~reyn.security.permissions.approval_ledger.migrate_legacy_snapshot`)
        — a no-op on every call after the first, for this resolver or any
        other process (idempotent by construction: the ledger existing at
        all is what makes it skip)."""
        if self.__saved is not None and self.__bound_identities is not None:
            return
        from reyn.security.permissions.approval_ledger import (
            ApprovalLedger,
            migrate_legacy_snapshot,
        )
        ledger = ApprovalLedger(self._approval_ledger_path)
        migrate_legacy_snapshot(ledger, self._approvals_path)
        self.__saved, self.__bound_identities = ledger.fold()

    def _path_identity(self, path: Path) -> "tuple[int, float | None] | None":
        """#5042/#5084: raw ``(st_ino, st_birthtime)`` of *path* (file OR
        directory — a path-flavor grant can name either) — the SAME shape
        ``AgentRegistry.agent_directory_identity`` (#5084) reads.
        ``st_birthtime`` is ``None`` on a platform that does not expose it
        (most Linux filesystems via plain ``stat()``); ``None`` overall
        only for the path genuinely not existing. This method makes no
        claim about what an ``st_birthtime``-less comparison MEANS — that
        interpretation (the 3-value confirmed/refuted/unconfirmable
        contract, per #5152) lives in :meth:`_identity_check_passes`, not
        here."""
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_ino, getattr(st, "st_birthtime", None))

    def _acquire_identity_fd(self, key: str, path: Path) -> None:
        """#5152 (architect ruling, issuecomment-5383604927): open and
        hold a file descriptor to *path*, for the remaining life of THIS
        process, from the moment *key*'s identity is (re)bound.

        Why this closes the ``st_birthtime``-absent gap entirely: POSIX
        keeps an inode allocated as long as ANY reference to it remains
        open, even after every directory entry naming it is removed
        (``rmdir``/``unlink``). So as long as this fd stays open, the
        filesystem cannot silently hand that SAME inode number to a
        brand-new object created at the same path — the exact ambiguity
        that made a bare ``ino`` comparison untrustworthy without
        ``st_birthtime`` (measured: Linux ext4/tmpfs routinely reuses a
        just-freed inode; this fd being held prevents that reuse for
        *this* inode specifically). A later ``fstat(fd)`` vs. a fresh
        ``stat(path)`` therefore gives a REAL confirmed match/mismatch,
        with or without ``st_birthtime`` — see :meth:`_identity_check_passes`.

        Does NOT survive a process restart (the fd table starts empty) —
        that residual window is the ``unconfirmable_identity_check_count``
        acceptance criterion, not closed by this method.

        #5157 (e2e-coder's TESTS-READY(B) finding on #5152, test-review
        Q5 — "what does this accumulate, and who bounds it?"). Architect
        ruling (issuecomment-5383671820): the POPULATION here is
        rate-limited by a human, not machine-driven growth — one fd per
        distinct PATH-flavor approval, and an approval is only ever
        created by a person granting it (reaching fd-table exhaustion,
        ~1024+, needs on the order of 1000 individual human approvals).
        An LRU cap was explicitly REJECTED: making eviction routine would
        demote a still-protected key into the "cannot confirm" bucket as
        a matter of course, making the pool SIZE decide the security
        guarantee's scope, not the approval's own lifecycle. The actual
        gap: a same-key REBIND was the ONLY release path this method
        had — closed by :meth:`_release_identity_fd`, called from
        :meth:`_persist`'s own revoke branch (the same #5146-style
        "settle on disappearance" idiom, here triggered by a human
        revoking the approval rather than a purge). ``bound_fd_count``
        is the public read Q5 asked for, so the real in-use count is
        observable if this population assumption ever needs revisiting.

        Best-effort: a failure to open (permissions, races, an
        already-exhausted fd table) just means this process falls back
        to the stat-only comparison for *key*, same as before this
        method existed."""
        self._release_identity_fd(key)
        try:
            self._bound_fds[key] = os.open(str(path), os.O_RDONLY)
        except OSError:
            pass

    def _release_identity_fd(self, key: str) -> None:
        """#5157: close and forget *key*'s identity fd, if one is held.
        Called on a rebind (a fresh fd replaces the old one) and from
        :meth:`_persist`'s revoke branch (the approval disappearing is
        exactly the disappearance-trigger #5146 already established for
        this class of state — nothing left to protect once the grant
        itself is gone)."""
        old = self._bound_fds.pop(key, None)
        if old is not None:
            try:
                os.close(old)
            except OSError:
                pass

    def _bind_identity(
        self,
        key: str,
        identity: "tuple[int, float | None]",
        approved_p: Path,
        *,
        persist: bool,
    ) -> None:
        """Record *key*'s bound identity — bind-ON-FIRST-USE (#5042
        architect ruling), never at approval time (an approval can predate
        the target existing at all; binding then would force a second,
        unnecessary prompt the first time the path shows up). ``persist``
        is False for a SESSION-only approval (never written to
        ``approvals.yaml`` in the first place — binding it there too would
        write disk state for a grant the user never asked to persist);
        True for a ``_saved`` (``ALWAYS``-choice) approval, written to the
        SAME file's own ``_bound_identities`` sibling key, never mixed
        into the approval row itself (see ``__bound_identities``'s own
        docstring for why). Also (re)acquires this process's identity fd
        for *key* (see :meth:`_acquire_identity_fd`) — binding and fd
        acquisition happen together so every caller gets both halves of
        the confirmation contract for free.

        #5153 (architect ruling, issuecomment-5383838646, superseding the
        architect co-vet issuecomment-5383499299 that FIRST flagged this
        write's frequency): this write fires far more often than
        ``_persist``'s own decision — ``_persist`` only runs when a HUMAN
        approves (rare); binding runs on every path-approval's FIRST USE
        per process, inside ordinary tool execution (routine). The
        original fix here was a snapshot tmp-file + atomic ``replace``
        (still correct against a mid-write CRASH), but that read-modify-
        write shape ALSO loses an update under concurrent WRITERS — not
        just a crash — because every writer needs momentary ownership of
        the WHOLE file to change even one key. #5153 replaces the
        snapshot entirely with an APPEND to :class:`~reyn.security.
        permissions.approval_ledger.ApprovalLedger` — see that module's
        own docstring for why append-only removes the need for ownership
        at all (a second, unrelated append to a DIFFERENT key never
        conflicts; two racing appends for the SAME key both land, and
        folding resolves the ordering deterministically)."""
        self._bound_identities[key] = identity
        self._acquire_identity_fd(key, approved_p)
        if not persist:
            return
        from reyn.security.permissions.approval_ledger import ApprovalLedger
        ApprovalLedger(self._approval_ledger_path).append_identity_bind(
            key, identity[0], identity[1],
        )

    def _persist(self, key: str, approved: bool) -> None:
        # #2248: approvals.yaml is PERSIST, not recovery-core — so NO config generation
        # recorded here (unlike mcp/cron/hooks/index). Approvals are a USER-authored security decision
        # (granted via a permission prompt; revoked via the web/CLI surfaces) — never
        # agent-authored — so a rewind must NOT revert them; they survive rewind like
        # `memory/`. Owner-confirmed reclassification (config → persist).
        self._saved[key] = approved
        self._session[key] = approved
        if not approved:
            # #5157 (architect ruling, issuecomment-5383671820, and a
            # confirm-item catch on THIS fix's own TESTS-READY(A),
            # issuecomment-5383698618): a human REVOKING this approval is
            # the disappearance-trigger for its identity fd, same idiom
            # as #5146's own settle-on-disappearance — nothing left to
            # protect once the grant itself is gone. The BOUND IDENTITY
            # RECORD is one more thing that must go with it, not just the
            # fd: leaving `_bound_identities[key]` behind after revoke
            # means a LATER re-approval of the SAME key starts with no fd
            # (just released) but a STALE stat recorded from before the
            # revoke -- if the target was deleted+recreated in between
            # and the new object's (ino, birthtime) happens to coincide
            # with the old one (inode reuse; or two creations landing in
            # the same coarse-grained birthtime tick), the stale record
            # would read as a CONFIRMED match — precisely the "a name is
            # not an identity" shape #5042 exists to close, reopened by
            # this PR's own fd-release path. Clearing the record forces
            # the next use back through bind-ON-FIRST-USE, same as a
            # never-before-seen key.
            self._release_identity_fd(key)
            self._bound_identities.pop(key, None)
        # #5153 (architect ruling, issuecomment-5383838646): append the
        # decision instead of read-modify-writing the whole snapshot — see
        # ``approval_ledger.py``'s own module docstring. A revoke's
        # in-memory fd-release/binding-clear above (#5157) is THIS
        # process's own state; the PERSISTED half of "a revoke clears the
        # bound identity too" is enforced generically by
        # ``ApprovalLedger.fold`` (any ``approved=False`` record clears
        # that key's fold-time binding, from WHICHEVER writer produced
        # it — CLI, web router, or here) — no extra write needed for it.
        from reyn.security.permissions.approval_ledger import ApprovalLedger
        ApprovalLedger(self._approval_ledger_path).append_approval(key, approved)
        # #398 v4 emitter wiring: notify subscribers (= Session
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
        written to ``approvals.yaml``. Used by Session to mint
        a ``state_change`` history entry per ``notify_state_change``
        so the LLM sees the permission update in its next turn
        (= directly mitigates the #352 in-context-learning refusal
        trap pattern).

        Multiple Sessions can register the same shared resolver
        so a project-wide grant notifies every active session
        independently.
        """
        self._on_persist_callbacks.append(callback)

    def unregister_on_persist(
        self, callback: Callable[[str, bool], None],
    ) -> bool:
        """Detach a previously registered callback.

        Returns True iff the callback was found and removed. Use this
        on Session shutdown to prevent dead-session callbacks from
        accumulating in long-running PermissionResolver instances
        (= the shared singleton model in ``reyn web`` / ``reyn run``
        sessions outlive individual Sessions).
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
        # Composite keys (e.g. "<actor>/python.safe/./mod.py:fn") accept
        # a kind-level blanket grant in config (e.g. "python.safe: allow").
        # Honor the same config blanket-grant here at the
        # runtime check so config and runtime stay consistent.
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

    def _is_path_approved_for(self, path: str, actor: str, kind: str) -> bool:
        """Return True if path is covered by any saved/session approval for this actor+kind.

        kind is "file.read" or "file.write".

        #5042: a matching grant is additionally checked against its OWN
        bound identity (``(st_ino, st_birthtime)`` of the APPROVED path
        itself, ``approved_p`` below — the resource the operator actually
        approved, not necessarily the deeper ``p_resolved`` being checked
        under a recursive grant). Bind-ON-FIRST-USE (architect ruling,
        issuecomment-5383453175): the first time a matching grant is found
        with no bound identity yet (a pre-#5042 legacy entry, or a fresh
        approval whose target did not exist at approval time), it is
        bound NOW to whatever exists at this moment — never at approval
        time, which can precede the target existing at all. Once bound, a
        LATER mismatch (the approved path was deleted and a different
        object now sits at the same name — #5042's own root finding, the
        #5084/#5146-class "a name is not an identity") means the grant no
        longer applies — the loop continues to the NEXT candidate key
        rather than returning False outright, so a different, still-valid
        grant covering the same path is not shadowed by one stale one."""
        base = self._project_root
        p = Path(path).expanduser()
        p_resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
        prefix = f"{actor}/{kind}/"
        combined = {**self._saved, **self._session}
        for key, approved in combined.items():
            if not approved or not key.startswith(prefix):
                continue
            approved_str = key[len(prefix):]
            # #2415: resolve the approved key against the SAME base as the check target
            # (``self._project_root``), NOT the CWD. approvals.yaml is project-scoped (under
            # ``.reyn/``), so a persisted relative grant like ``reyn/local/`` means "under the
            # project root". ``_expand`` resolved it CWD-relative, so a run whose cwd != project_root
            # (e.g. launched from a subdirectory) failed the ``relative_to`` match even with the
            # grant present in-memory — the recursive grant was silently not honored (#2415).
            approved_raw = Path(approved_str.rstrip("/")).expanduser()
            approved_p = (
                approved_raw.resolve() if approved_raw.is_absolute()
                else (base / approved_raw).resolve()
            )
            matched = False
            if approved_str.endswith("/"):
                try:
                    p_resolved.relative_to(approved_p)
                    matched = True
                except ValueError:
                    pass
            else:
                matched = p_resolved == approved_p
            if not matched:
                continue
            if self._identity_check_passes(key, approved_p):
                return True
            # else: this key matched by path but its bound identity is
            # stale — keep searching, do not fail closed on the WHOLE
            # check just because this one candidate is no longer valid.
        return False

    def _identity_check_passes(self, key: str, approved_p: Path) -> bool:
        """#5042: the identity half of a matched path-approval — see
        :meth:`_is_path_approved_for`'s own docstring for the bind-on-
        first-use / mismatch-means-keep-searching contract this serves.

        #5152 (architect ruling, issuecomment-5383604927 — RETRACTING
        issuecomment-5383544769's own literal fail-closed, which turned
        out to be over-broad, see below). This is a THREE-value question,
        not two: ① confirmed same → honor the grant; ② confirmed
        different → deny (#5042's own purpose); ③ cannot be confirmed
        either way → honor the grant AND count/log it, never silently
        fold ③ into ②. Folding ③ into ② (the retracted ruling) made
        every path approval effectively single-use on a platform without
        ``st_birthtime`` — measured regression (tui-coder): 6 unrelated
        tests failed, every one an ordinary same-session repeat use of an
        already-bound approval with NO purge at all; the fix's own log
        line fired on that second, unrelated use, proving ③ had been
        silently answered as ②.

        The fix that actually closes the ``st_birthtime`` gap without
        that collapse: :meth:`_acquire_identity_fd` holds an fd open on
        the approved path from bind-time onward, for the rest of THIS
        process's life. As long as that fd stays open, POSIX guarantees
        its inode cannot be silently handed to a replacement object, so
        ``fstat(fd)`` vs. a fresh ``stat(path)`` is a REAL confirmed
        match/mismatch (① or ②) — with or without ``st_birthtime`` — and
        this is the path taken for every use after the first in a given
        process. ③ now only remains for the genuinely unprotected window:
        the FIRST use after a process restart (the fd table starts
        empty, only the persisted ``(ino, birthtime)`` survived) on a
        platform where that pair alone can't confirm anything. The SAME
        defect (2-value collapse via bare ino) exists in
        ``AgentRegistry.agent_directory_identity`` (#5084) — out of
        scope here, re-filed separately (#5084/#5123) since that call
        site's safe direction differs by consumer.
        """
        bound = self._bound_identities.get(key)
        current = self._path_identity(approved_p)
        if bound is None:
            if current is not None:
                # Only a SAVED (ALWAYS-choice) approval's binding is
                # written to disk — a session-only (YES-choice, this-run-
                # only) approval was never persisted to approvals.yaml in
                # the first place, and binding it there too would write
                # disk state for a grant the user never asked to persist.
                self._bind_identity(key, current, approved_p, persist=key in self._saved)
            return True  # unbound (never used, or target doesn't exist yet) -- honor it
        if current is None:
            return False  # the approved target is gone -- fail-closed

        fd = self._bound_fds.get(key)
        if fd is not None:
            try:
                fd_ino = os.fstat(fd).st_ino
            except OSError:
                fd = None  # the fd itself went bad -- fall through below
            else:
                # ① / ② — a REAL confirmation, fd-anchored: this fd has
                # held its inode open since bind, so no replacement
                # object could have silently reused it.
                return fd_ino == current[0]

        # No fd protection for `key` in THIS process (most commonly: the
        # first use after a process restart). Fall back to the raw stat
        # comparison this PR originally shipped with.
        if bound[1] is not None and current[1] is not None:
            matched = current == bound  # ① or ② -- st_birthtime confirms it directly
        else:
            # ③ — cannot confirm. Honor the grant (never silently fold
            # into ②), count/log it, and (re)acquire an fd below so
            # every LATER use in this process is fd-anchored instead.
            self._unconfirmable_identity_checks += 1
            logger.warning(
                "PermissionResolver: %r's bound identity cannot be "
                "confirmed by stat alone (st_birthtime unavailable, no "
                "fd held by this process yet) -- honoring the grant and "
                "anchoring an fd for the rest of this process's life",
                key,
            )
            matched = True
        if matched:
            self._bind_identity(key, current, approved_p, persist=key in self._saved)
        return matched

    # Backwards-compatible alias used by older write-class call sites.
    def _is_path_approved(self, path: str, actor: str) -> bool:
        return self._is_path_approved_for(path, actor, "file.write")

    def _resolve_for_offload(self, path: str) -> str:
        """Resolve ``path`` to an absolute string (same convention as the gate)."""
        p = Path(path).expanduser()
        return str((self._project_root / p).resolve() if not p.is_absolute() else p.resolve())

    def grant_offload_read(self, path: str) -> None:
        """Register a scoped read-grant for an OS-offloaded artifact path (#1383 D12).

        Called by the offload-emit layer (``context_builder`` artifact_ref /
        offload_value) the moment a state-dir path is handed to the agent as a
        readable ref. Grants read on EXACTLY this path (resolved) — not the
        containing dir — so least-privilege holds. The read gate
        (:meth:`require_file_read`) consults :meth:`_is_offload_read_granted`.
        """
        self._offload_read_paths.add(self._resolve_for_offload(path))

    def _is_offload_read_granted(self, path: str) -> bool:
        """True if ``path`` was registered via :meth:`grant_offload_read` (exact match)."""
        return self._resolve_for_offload(path) in self._offload_read_paths

    def _read_base_approved(self, path: str) -> bool:
        """Read-approval shared by BOTH read gates (#1383 follow-up).

        The op-runtime gate (:meth:`require_file_read`) and the Workspace gate
        (:meth:`is_read_allowed`) are documented to make the SAME decision. The
        offload grant is the part they MUST agree on, so it lives here — a
        single source both gates call, so the offload-grant decision cannot
        diverge (the merged D12 bug: only one gate had it). The per-actor
        path-approval term differs (is_read_allowed guards it on a non-empty
        actor) and stays inline in each gate.

        #3458: the config grant (``file.read: allow``) left this helper — it is
        one of the forms :meth:`file_scopes` resolves, so ``permissions.file.*``
        is read in exactly one place.
        """
        return self._is_offload_read_granted(path)

    def _is_host_approved_for(
        self, host: str, actor: str, kind: str = "http.get",
    ) -> bool:
        """Return True if ``host`` is covered by a saved/session approval.

        Hosts are exact-string-matched against the persisted approval
        key (= ``<actor>/http.get/<host>``). Mirrors
        :meth:`_is_path_approved_for` but skips the filesystem
        resolution because hosts are network identifiers, not paths.
        """
        if not actor or not host:
            return False
        key = f"{actor}/{kind}/{host}"
        return bool(self._saved.get(key) or self._session.get(key))

    def session_approve_path(
        self, path: str, actor: str, kind: str, recursive: bool = False,
    ) -> None:
        """Mark `path` as approved for this session only (not persisted).

        Used to suppress the runtime prompt for paths the caller wants to
        silently approve up-front (avoids an interactive prompt before the
        chat REPL takes over stdin).

        kind: "file.read" or "file.write". When recursive=True the approval
        covers the directory and everything beneath it.
        """
        # #2415: resolve against ``self._project_root`` (the base ``_is_path_approved_for`` uses),
        # NOT the CWD. A relative path (e.g. a project-scoped declared grant) resolved CWD-relative
        # would store a key that never matches the project_root-anchored check target when
        # cwd != project_root — the same cwd-anchor class fixed in ``_is_path_approved_for``. Absolute
        # inputs are unchanged (already CWD-independent).
        raw = Path(path).expanduser()
        p = str(raw.resolve() if raw.is_absolute() else (self._project_root / raw).resolve())
        if recursive:
            p = p.rstrip("/") + "/"
        self._session[f"{actor}/{kind}/{p}"] = True

    def session_approve_host(
        self, host: str, actor: str, kind: str = "http.get",
    ) -> None:
        """Mark ``host`` as approved for this session only (not persisted).

        Sibling of :meth:`session_approve_path` for the ``http.get`` axis
        (#571 Phase 7). Hosts are network identifiers, not paths, so they
        do not go through ``_expand`` / filesystem resolution. Persistence
        key matches what :meth:`_is_host_approved_for` reads, so tests
        and operator-startup code can pre-seed approvals via this public
        surface instead of mutating ``_session`` directly.
        """
        if not actor or not host:
            return
        self._session[f"{actor}/{kind}/{host}"] = True

    async def _prompt_file_access(
        self, path: str, scope: str, actor: str, kind: str, bus: RequestBus,
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
        # #2415: honor the DECLARED scope on approval. When the actor declared this path as
        # ``recursive`` (confirming a declared grant), an affirmative approval grants
        # the declared RECURSIVE dir — the operator approves/denies the declared grant, they do not
        # re-scope it narrower. Otherwise (a JIT prompt, or a ``just_path`` declaration) an
        # affirmative grants the exact path. Without this, an actor that declares ``reyn/local``
        # recursive but writes ``reyn/local/{name}/…`` (a SUBPATH) was silently denied unless the
        # operator happened to pick [r] — the declared recursive intent was not honored (#2415).
        affirmative_key = recursive_target if scope == "recursive" else path
        if choice == YES:
            self._session[f"{actor}/{kind}/{affirmative_key}"] = True
            return True
        if choice == JUST_PATH:
            self._persist(f"{actor}/{kind}/{affirmative_key}", True)
            return True
        if choice == RECURSIVE:
            self._persist(f"{actor}/{kind}/{recursive_target}", True)
            return True
        # NO or unknown → deny (session-only)
        self._session[f"{actor}/{kind}/{path}"] = False
        return False

    # ── Public check methods ──────────────────────────────────────────────────

    async def require_file_read(
        self,
        decl: PermissionDecl,
        path: str,
        actor: str = "",
        *,
        sandbox_policy: "SandboxPolicy | None" = None,
        bus: "RequestBus | None" = None,
    ) -> None:
        """
        Raise PermissionError if read/glob/grep access to path is not allowed.

        #3458: the permitted set is ``permissions.file.read`` resolved by
        :meth:`file_scopes` — unset = the schema default (``<zone-root>`` and
        below), ``deny`` = the empty set, a path list = exactly that set. The
        advertisement side reads the SAME resolution, so what the model is told
        and what this gate enforces cannot drift.

        Outside that set: ask via ``bus`` when provided (JIT prompt, mirrors
        ``require_http_get``); deny when bus=None (non-interactive).

        Config ``file.read: deny`` still suppresses the JIT ask too.

        #1505: async-ified + JIT ask — outside-scope access now prompts the
        user at gate time (bus≠None) instead of hard-denying. bus=None
        (non-interactive / eval) preserves the prior deny behavior.

        #1199 S3.1c-1: decl-less (scope OR approved). #3901 PR-B ③: FILE_READ/
        FILE_WRITE no longer participate in SandboxLayer's permission-∩
        projection (an operator cannot know a sandbox's path floor, so it is
        no longer treated as permission) — the ``SandboxLayer(sandbox_policy)``
        below still joins the ∩ for SUBPROCESS/ENV, but is ⊤ (a no-op) on
        this axis; ``sandbox_policy`` is kept as a parameter only so callers
        that pass one for those other axes are unaffected.
        """
        scopes = self.file_scopes()
        # Config-tier deny always wins — it suppresses even the JIT ask.
        if scopes.read.is_denied:
            raise PermissionError(
                f"read from '{path}' denied by config (file.read: deny)."
            )

        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            EffectivePermission,
            SandboxLayer,
        )

        def _approved(axis: object, value: object) -> bool:
            # #1383: config + offload grant via the shared base (kept in sync with
            # is_read_allowed); per-actor path-approval inline.
            return self._read_base_approved(str(value)) or self._is_path_approved_for(
                str(value), actor, "file.read"
            )

        if EffectivePermission([
            AgentLayer(decl, approval_check=_approved,
                       file_zone_root=self._file_zone_root,  # #1414
                       file_scopes=scopes),  # #3458
            SandboxLayer(sandbox_policy),
        ]).allows(CapabilityAxis.FILE_READ, path):
            return

        # JIT ask: outside scope, not yet approved. Mirrors require_http_get wildcard path.
        if bus is not None:
            if await self._prompt_file_access(path, "just_path", actor, "file.read", bus):
                return

        raise PermissionError(
            f"read from '{path}' was not approved (declared but not granted).\n"
            f"Why: it is outside the configured read scope "
            f"({', '.join(scopes.read.advertised_paths) or 'empty'}) and has no approval.\n"
            f"Options:\n"
            f"  - pre-approve in reyn.yaml: permissions.file.read: allow (or the "
            f"specific path), then re-run; or\n"
            f"  - run interactively — a prompt will appear at the time of access."
        )

    async def require_file_write(
        self,
        decl: PermissionDecl,
        path: str,
        actor: str = "",
        *,
        sandbox_policy: "SandboxPolicy | None" = None,
        bus: "RequestBus | None" = None,
    ) -> None:
        """
        Raise PermissionError if write/edit/delete access to path is not allowed.

        #3458: the permitted set is ``permissions.file.write`` resolved by
        :meth:`file_scopes` — unset = the schema default (``<zone-root>/.reyn``,
        minus the protected carve-outs), ``deny`` = the empty set, a path list =
        exactly that set. The narrowness of the write default is now visible in
        the configuration rather than only in this docstring.

        Outside that set: ask via ``bus`` when provided (JIT prompt, mirrors
        ``require_http_get``); deny when bus=None (non-interactive).

        Config ``file.write: deny`` still suppresses the JIT ask too.

        #1505: async-ified + JIT ask — outside-scope writes now prompt the
        user at gate time (bus≠None) instead of hard-denying. bus=None
        (non-interactive / eval) preserves the prior deny behavior.

        #1199 S3.1c-1: decl-less (scope OR approved). #3901 PR-B ③: FILE_READ/
        FILE_WRITE no longer participate in SandboxLayer's permission-∩
        projection (an operator cannot know a sandbox's path floor, so it is
        no longer treated as permission) — the ``SandboxLayer(sandbox_policy)``
        below still joins the ∩ for SUBPROCESS/ENV, but is ⊤ (a no-op) on
        this axis; ``sandbox_policy`` is kept as a parameter only so callers
        that pass one for those other axes are unaffected.
        """
        scopes = self.file_scopes()
        # Config-tier deny always wins — it suppresses even the JIT ask.
        if scopes.write.is_denied:
            raise PermissionError(
                f"write to '{path}' denied by config (file.write: deny)."
            )

        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            EffectivePermission,
            SandboxLayer,
        )

        def _approved(axis: object, value: object) -> bool:
            # #3458: the config grant (``file.write: allow``) is no longer read
            # here — it is one of the forms ``file_scopes()`` resolves, so the
            # config is consulted in exactly one place. Only the per-actor
            # runtime approvals remain.
            return self._is_path_approved_for(str(value), actor, "file.write")

        if EffectivePermission([
            AgentLayer(decl, approval_check=_approved,
                       file_zone_root=self._file_zone_root,  # #1414
                       file_scopes=scopes),  # #3458
            SandboxLayer(sandbox_policy),
        ]).allows(CapabilityAxis.FILE_WRITE, path):
            return

        # JIT ask: outside scope, not yet approved. Mirrors require_http_get wildcard path.
        if bus is not None:
            if await self._prompt_file_access(path, "just_path", actor, "file.write", bus):
                return

        raise PermissionError(
            f"write to '{path}' was not approved (declared but not granted).\n"
            f"Why: it is outside the configured write scope "
            f"({', '.join(scopes.write.advertised_paths) or 'empty'}) and has no "
            f"approval.\n"
            f"Options:\n"
            f"  - pre-approve in reyn.yaml: permissions.file.write: allow (or the "
            f"specific path), then re-run; or\n"
            f"  - run interactively — a prompt will appear at the time of access."
        )

    async def require_http_get(
        self,
        decl: PermissionDecl,
        host: str,
        bus: "RequestBus | None" = None,
        actor: str = "",
        *,
        sandbox_policy: "SandboxPolicy | None" = None,
    ) -> None:
        """Gate HTTP access to ``host`` (#571 Phase 7 unification).

        Mirrors the ``file.write`` model — declaration is intent, the
        prompt fires at the timing where the host actually becomes
        known:

        - **Specific declared host** (``http.get: [{host: "api.github.com"}]``):
          the runtime prompt fires once per host and persists the
          decision to approvals.yaml under ``<actor>/http.get/<host>``.
          A subsequent run is then silent — this method finds the
          persisted approval and passes.
        - **Wildcard** (``http.get: [{host: "*"}]`` or ``["*"]``): host
          set is unknown at write-time (= LLM picks at runtime), so
          the prompt fires here at the actual host gate. Same
          ``<actor>/http.get/<host>`` persistence; ALWAYS / NEVER
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

        # #1199 S3.1c-2: SandboxLayer ∩ network. The sandbox is a RESTRICT layer,
        # so it must veto BEFORE the AgentLayer GRANT tiers below (config-allow /
        # persisted / legacy) — a config-allowed host must NOT bypass a sandbox
        # that disallows network. Placed after config-DENY (deny still wins) and
        # before every allow tier. ``None`` (non-sandboxed callers) → no veto.
        from reyn.security.permissions.effective import (
            CapabilityAxis as _AX,
        )
        from reyn.security.permissions.effective import (
            EffectivePermission as _EP,
        )
        from reyn.security.permissions.effective import (
            SandboxLayer as _SL,
        )

        if not _EP([_SL(sandbox_policy)]).allows(_AX.NETWORK_HOST, host):
            raise PermissionError(
                f"HTTP access to host {host!r} denied by the active sandbox "
                f"policy (network access is disabled). This is a sandbox "
                f"restriction — it overrides any config/declared allow. Adjust "
                f"the phase ``default_sandbox_policy`` (``network: true``) if the "
                f"host should be reachable."
            )

        # Config-tier allow short-circuits everything (= operator's
        # blanket pre-approval — present today as ``web.fetch: allow``).
        if self._is_config_approved("web.fetch"):
            return
        if self._is_config_approved(f"http.get.{host}"):
            return

        # Persisted per-host approval (from a prior interactive
        # runtime prompt, specific or wildcard).
        if actor and self._is_host_approved_for(host, actor, "http.get"):
            return
        # Legacy session/saved ``web.fetch`` approval still authorises
        # every host while the deprecation window is open.
        if self._saved.get("web.fetch") or self._session.get("web.fetch"):
            return

        # #1199 S3.1b-2c-2: the host-MEMBERSHIP decision (specific OR wildcard)
        # routes through the unified model (NETWORK_HOST axis) — pure decl
        # membership (no approval_check; the config/persisted/legacy approvals are
        # the separate disjuncts above). Byte-identical to the prior
        # `has_specific OR has_wildcard`. This is the seam S3.1c uses to ∩
        # SandboxLayer.network (closing the gap where a sandboxed actor's http_get
        # is not network-bound today). has_wildcard stays local for the prompt label.
        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            EffectivePermission,
        )

        has_wildcard = any(
            isinstance(e, dict) and e.get("host") == "*" for e in decl.http_get
        )

        if EffectivePermission([AgentLayer(decl)]).allows(
            CapabilityAxis.NETWORK_HOST, host
        ):
            # Need to prompt — a still-unapproved host (a specific decl
            # not yet approved, or the wildcard JIT path).
            if bus is None:
                raise PermissionError(
                    f"HTTP access to host {host!r} requires an interactive "
                    f"prompt but no bus is available. Pre-approve via "
                    f"reyn.yaml (`permissions.web.fetch: allow` for blanket, "
                    f"or run interactively so the prompt can collect "
                    f"approvals)."
                )
            approval_key = f"{actor}/http.get/{host}"
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
        # for the segmented migration window. Actors that previously
        # relied on the Tier-1 default-allow behaviour still work
        # while we wait for them to declare ``http.get`` explicitly.
        import warnings
        warnings.warn(
            f"HTTP access to host {host!r} from actor {actor!r} "
            f"without an http.get declaration. This will become a hard "
            f"error in a future release. Add to reyn.yaml permissions:\n"
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

    async def require_plugin_git_run_code_trust(
        self,
        url: str,
        bus: "RequestBus | None",
        actor: str = "",
    ) -> None:
        """Per-install operator-trust gate for installing + RUNNING remote
        plugin code from a git source (ADR 0064 §3.10 item 3).

        **DISTINCT from ``require_http_get`` (the fetch axis).** Fetching bytes
        from a host and RUNNING code from that host are different trust
        decisions. ``require_http_get`` is per-host, PERSISTENT (ALWAYS →
        ``approvals.yaml``), and SHARED with ``web.fetch`` — so a host approved
        once for a web fetch would, if it also gated plugin install, become a
        standing silent-RCE grant: any later ``{kind:git}`` plugin from that
        host installs + runs with no prompt. This gate closes that hole by
        being a SEPARATE axis that a fetch/http.get approval can never satisfy.

        **Per-install, NEVER auto-run, NEVER persisted.** This method
        deliberately does NOT consult (or write) ``approvals.yaml`` / the
        session-approval map / config ``allow`` at all — there is no key, no
        ALWAYS path, no ``reyn.yaml`` pre-grant. Every ``{kind:git}`` install
        re-asks, because "never auto-run" (§3.10) means the decision cannot be
        pre-made. The choice set (``plugin_run_code_trust_choices``) offers
        only yes/no, so the UI cannot even present a persist option — the
        non-persistence is structural, not a convention.

        Fail-closed: a non-interactive caller (``bus is None`` OR
        ``self._interactive is False``) DENIES — remote code is never run
        without an explicit, live operator yes.
        """
        from reyn.intervention_choices import plugin_run_code_trust_choices

        if bus is None or not self._interactive:
            raise PermissionError(
                f"installing + running remote plugin code from git source "
                f"{url!r} requires an explicit operator-trust decision, which "
                f"cannot be made non-interactively.\n"
                f"Why: fetching and RUNNING remote code is an RCE trust "
                f"boundary (ADR 0064 §3.10) — it is deliberately NOT "
                f"pre-grantable (no reyn.yaml allow, no persisted approval), so "
                f"a run without a live prompt is refused.\n"
                f"Run interactively and approve the install-and-run prompt, or "
                f"use a {{kind:local}} source (already-on-disk code) instead."
            )
        iv = UserIntervention(
            kind="permission.plugin_git_run_code_trust",
            prompt=(
                f"Install AND RUN plugin code from remote git source {url!r}?"
            ),
            detail=(
                f"This fetches code from {url!r} and registers it to run in "
                f"future sessions (an MCP server / pipeline / skill). Only "
                f"approve a source you trust to run code on this machine. This "
                f"decision is asked FRESH every install — it is never saved."
            ),
            choices=plugin_run_code_trust_choices(),
        )
        answer = await bus.request(iv)
        if answer.choice_id == YES:
            return
        raise PermissionError(
            f"installing + running remote plugin code from git source {url!r} "
            f"was declined by the operator (run-code trust not granted)."
        )

    def require_secret_write(
        self, decl: PermissionDecl, key: str, actor: str = "",
    ) -> None:
        """Raise PermissionError if secret-store write of ``key`` is not declared.

        Two declaration shapes are accepted:

        - **Specific key** — ``secret.write: ["GITHUB_TOKEN"]`` authorises
          only that exact key. Use when the actor knows at write-time
          which env-var names it will save.
        - **Wildcard** ``"*"`` — ``secret.write: ["*"]`` authorises any
          key. Use when the key set is determined at runtime from
          external metadata (= ``mcp_install``'s ``isSecret``
          environment variables from the registry response). The
          security gate in this case is the operator's per-value prompt
          at op-execution time; the wildcard declaration is the
          author's acknowledgement that the actor will route through
          that prompt-then-save flow.

        Specific entries take precedence — an actor that lists both
        ``"GITHUB_TOKEN"`` and ``"*"`` is functionally equivalent to
        just ``"*"`` but conveys intent more clearly.
        """
        # #1199 S3.1b-2c: the static secret-write authority (specific key OR "*"
        # wildcard) flows through the unified model (SECRET_WRITE axis).
        # Byte-identical. The operator's per-value op-execution prompt (for the
        # wildcard) is the separate runtime gate, unchanged.
        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            EffectivePermission,
        )

        if EffectivePermission([AgentLayer(decl)]).allows(
            CapabilityAxis.SECRET_WRITE, key
        ):
            return
        raise PermissionError(
            f"Secret-store write of key {key!r} not declared in actor permissions. "
            f"Add to reyn.yaml permissions:\n"
            f"  permissions:\n"
            f"    secret.write:\n"
            f"      - {key}\n"
            f"or use the wildcard form for runtime-determined keys:\n"
            f"  permissions:\n"
            f"    secret.write:\n"
            f"      - '*'\n"
        )

    def is_env_expand_allowed(self, decl: PermissionDecl, name: str) -> bool:
        """True iff ``${env:name}`` may be expanded from ``os.environ`` for
        this actor's declared permissions (#3198).

        NON-raising by design (unlike ``require_secret_write``): the caller
        (``reyn.plugins.skill_load``) treats a denial as "leave the token
        unexpanded", not a hard error — a skill body routinely mixes
        several ``${env:...}`` tokens, only some of which may be declared,
        and the read op must still succeed for the rest of the file.

        Thin wrapper over the module-level :func:`env_expand_allowed` (which
        needs only a ``PermissionDecl``, not a live resolver instance —
        ``reyn.plugins.skill_load`` calls that directly to avoid
        constructing a throwaway ``PermissionResolver``, whose ``__init__``
        does real ``.reyn/approvals.yaml`` I/O, on every skill-body read).
        """
        return env_expand_allowed(decl, name)

    def is_read_allowed(self, path: str, actor: str = "") -> bool:
        """Check if reading `path` is allowed.

        Allowed if: the path is in the configured read scope (#3458 —
        ``permissions.file.read``, schema-defaulting to ``<zone-root>`` and
        below), OR a per-actor approval covers it.

        #1199 S3.1b-2b / S3.1c-1 (the Workspace read gate): routed through the
        unified EffectivePermission model — a decl-less AgentLayer (zone OR
        approved). As of S3.1c-1 the op-runtime ``require_file_read`` gate is ALSO
        decl-less, so the two now make the SAME decision (the S3.1b-2 transitional
        divergence is resolved).
        """
        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            EffectivePermission,
        )

        def _approved(axis: object, value: object) -> bool:
            # #1383 follow-up: config + offload grant via the shared base (same
            # source as require_file_read → the offload decision cannot diverge;
            # the merged D12 bug was this gate lacking the offload grant, leaving
            # astropy-13236 denied at Workspace._resolve_read). Per-actor
            # path-approval inline (guarded on a non-empty actor here).
            return self._read_base_approved(str(value)) or (
                bool(actor)
                and self._is_path_approved_for(str(value), actor, "file.read")
            )

        return EffectivePermission([
            AgentLayer(PermissionDecl(), approval_check=_approved,
                       file_zone_root=self._file_zone_root,  # #1414
                       file_scopes=self.file_scopes())  # #3458
        ]).allows(CapabilityAxis.FILE_READ, path)

    def is_write_allowed(self, path: str, actor: str = "") -> bool:
        """Check if writing `path` is allowed.

        Allowed if: the path is in the configured write scope (#3458 —
        ``permissions.file.write``, schema-defaulting to ``<zone-root>/.reyn``
        minus the protected carve-outs), OR a per-actor approval covers it.

        #1199 S3.1b-2a / S3.1c-1 (the Workspace write gate): routed through the
        unified EffectivePermission model — the single conjunctive-∩ source. A
        decl-less AgentLayer (zone OR approved). As of S3.1c-1 the op-runtime
        ``require_file_write`` gate is ALSO decl-less, so the two now make the
        SAME decision (the S3.1b-2 transitional divergence is resolved). The
        config/path approvals fold INSIDE the layer (② grant-back-safe).
        """
        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            EffectivePermission,
        )

        def _approved(axis: object, value: object) -> bool:
            # #3458: ``file.write: allow`` is resolved by ``file_scopes()`` now.
            return bool(actor) and self._is_path_approved_for(
                str(value), actor, "file.write",
            )

        return EffectivePermission([
            AgentLayer(PermissionDecl(), approval_check=_approved,
                       file_zone_root=self._file_zone_root,  # #1414
                       file_scopes=self.file_scopes())  # #3458
        ]).allows(CapabilityAxis.FILE_WRITE, path)

    async def require_mcp(
        self, decl: PermissionDecl, server: str, bus: RequestBus,
        *, contextual: "object | None" = None,
    ) -> None:
        # #1199 S3.1b: the static MCP authority flows through the unified
        # EffectivePermission ∩. #2074 S4a unifies the per-agent MCP allowlist
        # into a ``ProfileLayer`` (symmetric with the AGENT axis), and adds an
        # optional per-session ``ContextualLayer`` (MCP contextual narrowing,
        # ⊤-when-unset). The full ∩ is now:
        #   AgentLayer(decl.mcp grant) ∩ ProfileLayer(decl.allowed_mcp allowlist)
        #     ∩ ContextualLayer(contextual)
        # which is byte-identical to the prior ``grant ∩ allowlist`` when
        # ``contextual`` does not narrow MCP (∩ associative). The three diagnostics
        # below distinguish the failing layer (mirrors require_tool), preserving
        # the original allowlist + declared messages exactly. Local import avoids
        # the effective.py → permissions.py circular. ``_approve`` remains the
        # separate runtime gate (not part of the ∩).
        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            ContextualLayer,
            EffectivePermission,
            ProfileLayer,
            contextual_deny_message,
        )

        layers: list = [
            AgentLayer(decl),
            ProfileLayer.from_allowlists(allowed_mcp=decl.allowed_mcp),
        ]
        if contextual is not None:
            layers.append(ContextualLayer(contextual))
        if not EffectivePermission(layers).allows(CapabilityAxis.MCP, server):
            # Decision-enabling deny, distinguishing the failing layer (order
            # preserves the pre-S4a allowlist-then-declared messages byte-identically;
            # the contextual branch is NEW + only fires when a context narrows MCP):
            # 1. per-agent allowlist (ProfileLayer) — "not in allowed_mcp"
            if decl.allowed_mcp is not None and server not in decl.allowed_mcp:
                raise PermissionError(
                    f"MCP server {server!r} not in allowed_mcp for caller "
                    f"(agent allowlist exhausted)"
                )
            # 2. per-session contextual narrowing (ContextualLayer) — NEW (#2074 S4a)
            if contextual is not None and not ContextualLayer(contextual).allows(
                CapabilityAxis.MCP, server
            ):
                # #3501: same shared builder as the TOOL axis, on the MCP axis — so
                # the two gates cannot drift into differently-informative denies for
                # the same class of decision.
                raise PermissionError(
                    contextual_deny_message(
                        "MCP server", server, contextual, CapabilityAxis.MCP,
                    )
                )
            # 3. per-actor grant (AgentLayer) — "not declared in actor permissions"
            raise PermissionError(
                f"MCP server {server!r} not declared in actor permissions. "
                f"Add `permissions:\\n  mcp: [{server}]` to reyn.yaml permissions."
            )
        if not await self._approve(
            f"mcp.{server}",
            f"MCP server: {server!r}",
            bus,
            user_prompt=f"Allow access to MCP server {server!r}?",
        ):
            # Decision-enabling deny (was a bare "access denied"): name the
            # server and the two concrete ways to grant it — either
            # declaring/installing the server so it's CONFIGURED (which
            # `reyn pipe run` auto-grants — see pipe.py's
            # `_grant_configured_mcp_servers`), or a one-off explicit
            # config grant. Both routes covered so this fires the same for
            # an unconfigured server AND a configured-but-explicitly-denied
            # one.
            raise PermissionError(
                f"MCP server {server!r} access denied. To grant it: "
                f"(1) configure the server — add it to "
                f".reyn/config/mcp.yaml, or run `reyn mcp install {server}` "
                f"(a `reyn pipe run` invocation auto-grants any server it "
                f"finds configured there), or "
                f"(2) grant it explicitly — add `permissions:\\n  mcp:\\n"
                f"    {server}: allow` to reyn.yaml. See "
                f"docs/reference/config/permissions.md."
            )

    async def require_tool(
        self, decl: PermissionDecl, tool: str, bus: RequestBus,
        *, contextual: "object | None" = None,
    ) -> None:
        # #1199 S3.1b-2c: the static tool authority (decl.tool) flows through the
        # unified model (TOOL axis). Byte-identical; the _approve prompt remains.
        #
        # #1827 S1: an optional per-session ``contextual`` (a ``ContextualPermission``)
        # is added as one more restrict-only ∩ layer (``ContextualLayer``). The
        # decision is the structural ``all()`` over the layer stack — a contextual
        # deny cannot be re-granted, and the contextual layer cannot re-grant the
        # static authority's deny (never-elevate). ``contextual=None`` → the stack
        # is exactly ``[AgentLayer(decl)]`` = byte-identical to the pre-#1827 gate.
        from reyn.security.permissions.effective import (
            AgentLayer,
            CapabilityAxis,
            ContextualLayer,
            EffectivePermission,
            contextual_deny_message,
        )

        layers: list = [AgentLayer(decl)]
        if contextual is not None:
            layers.append(ContextualLayer(contextual))
        if not EffectivePermission(layers).allows(CapabilityAxis.TOOL, tool):
            # Decision-enabling deny: distinguish "static authority never granted
            # it" (declare it) from "the active capability context narrowed it
            # away" (the tool IS declared, but delegation/topology/ephemeral
            # narrowing blocks it) — without ever re-granting either.
            static_ok = EffectivePermission([AgentLayer(decl)]).allows(
                CapabilityAxis.TOOL, tool
            )
            if static_ok:
                # #3501: name WHICH narrowing, why, and what lifts it. Listing the
                # three candidate narrowings — as this did — is not decision-enabling:
                # the caller still cannot tell which one fired, and an LLM handed that
                # string cannot explain the loss or act on it.
                raise PermissionError(
                    contextual_deny_message("tool", tool, contextual)
                    + " It IS declared in actor permissions — the static authority "
                    "grants it; only the narrowing above removes it."
                )
            raise PermissionError(
                f"tool {tool!r} not declared in actor permissions. "
                f"Add `permissions:\\n  tool: [{tool}]` to reyn.yaml permissions."
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
        context from web_fetch / read_file / MCP / user input.

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

        The shared infrastructure is reused by #365 (read_file binary) and
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
