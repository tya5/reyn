"""The single source for "which paths are readable / writable" (#3458).

Before this module the question had **two** answers. The runtime gate
(``require_file_read`` → ``AgentLayer``) read a default zone hardcoded inside
the gate; the advertisement side (``router_tools.build_tools``) read only the
operator's ``permissions.file.*`` config value. With the config empty the gate
said "the project root is readable" and the advertisement said "nothing is
permitted", so the model was never told about a capability it actually had
(#3449 is one symptom).

The fix is structural, not a reconciliation: the default set moves **upstream
into the configuration schema**, and everybody — gate, advertisement, and any
future subsystem — obtains the set by calling :func:`resolve_file_scope`.

Symbols, not literals
---------------------
The zone root is a *function of the environment*, not a constant:
``PermissionResolver(file_zone_root=…)`` is fed ``ws_base_dir`` from chat / web
and ``project_root`` from pipe / plugin / registry bootstrap. A literal path
therefore cannot be written into a config default — its value is not known at
load time. So the configuration carries a **typed symbol** and this module
owns the one function that resolves it:

``permissions.file.read``   default → ``(ZoneRoot(),)``          rendered ``<zone-root>``
``permissions.file.write``  default → ``(ZoneStateDir(),)``      rendered ``<zone-root>/.reyn``

The symbols are a discriminated union (``kind`` marker), never a bare magic
string: ``"project_root"`` as a plain string would silently mis-resolve the day
the zone becomes a different concept.

Three ways to write one concept
-------------------------------
A path list *is* the permission, so the axis has exactly three config forms:

===================  ==========================================================
unset                the schema default (:data:`FILE_SCOPE_SCHEMA`)
``deny``             the empty set — and, as before, JIT asking is suppressed
``[path, …]``        exactly that set (it REPLACES the default, it is not
                     unioned with it — the operator is stating the set)
===================  ==========================================================

``allow`` remains the "everywhere" form (:class:`Anywhere`).

The JIT layer is unchanged (#3458 acceptance ④): a path outside the resolved
set is asked about when a ``bus`` is present and denied when it is not. Config
= the standing set; JIT = the per-access extension of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from reyn.security.permissions.permissions import (
    _in_default_read_zone,
    _in_default_write_zone,
)

# ── The typed symbols ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ZoneRoot:
    """Symbol: the file-zone root and everything under it.

    Resolved against the caller-supplied zone anchor (``file_zone_root`` —
    ``ws_base_dir`` under chat/web, ``project_root`` elsewhere), never against a
    constant baked into a gate."""

    kind: Literal["zone_root"] = "zone_root"


@dataclass(frozen=True)
class ZoneStateDir:
    """Symbol: ``<zone-root>/.reyn`` minus the protected carve-outs.

    Distinct from ``ZoneRelative(".reyn")`` on purpose: the carve-outs
    (``.reyn/approvals.yaml``; the ``.reyn/config/`` + ``.reyn/state/``
    recovery-core prefixes) are part of what this symbol *means*. A write to a
    carved-out path must ride its dedicated WAL-emitting op or an explicit
    grant, never the broad state-dir zone."""

    kind: Literal["zone_state_dir"] = "zone_state_dir"


@dataclass(frozen=True)
class LiteralPath:
    """An operator-written path. Relative entries resolve against the zone root."""

    path: str
    scope: Literal["just_path", "recursive"] = "recursive"
    kind: Literal["literal"] = "literal"


@dataclass(frozen=True)
class Anywhere:
    """Symbol for the ``allow`` form — no path restriction on this axis."""

    kind: Literal["anywhere"] = "anywhere"


FileScopeEntry = ZoneRoot | ZoneStateDir | LiteralPath | Anywhere


class FileScopeAxis(Enum):
    """The two path-set axes. The value is the config key."""

    READ = "file.read"
    WRITE = "file.write"


# ── The schema (where the default lives) ─────────────────────────────────────


@dataclass(frozen=True)
class FileScopeAxisSchema:
    """The declared schema of one ``permissions.file.*`` axis.

    The default lives HERE — in the definition of the configuration — rather
    than inside a runtime object's ``__init__`` (invisible to any reader that
    does not build that object = exactly the #3458 defect) or inside the config
    loader (invisible to a reader handed the raw dict)."""

    axis: FileScopeAxis
    default: tuple[FileScopeEntry, ...]
    description: str


FILE_SCOPE_SCHEMA: dict[FileScopeAxis, FileScopeAxisSchema] = {
    FileScopeAxis.READ: FileScopeAxisSchema(
        axis=FileScopeAxis.READ,
        default=(ZoneRoot(),),
        description="Paths readable without a per-access approval.",
    ),
    FileScopeAxis.WRITE: FileScopeAxisSchema(
        axis=FileScopeAxis.WRITE,
        default=(ZoneStateDir(),),
        description="Paths writable without a per-access approval.",
    ),
}


# ── Resolution — the ONE function every caller goes through ──────────────────


def _config_word(config: dict | None, axis: FileScopeAxis, word: str) -> bool:
    """True when config sets this axis (or its ``file`` parent) to ``word``.

    Mirrors the key lookup the resolver's ``_is_config_approved`` /
    ``_is_config_denied`` have always used: the dotted key, the parent key, and
    the nested ``file: {read: …}`` form."""
    if not isinstance(config, dict):
        return False
    key = axis.value
    if config.get(key) == word:
        return True
    top, sub = key.split(".", 1)
    parent = config.get(top)
    if parent == word:
        return True
    return isinstance(parent, dict) and parent.get(sub) == word


def _config_raw_list(config: dict | None, axis: FileScopeAxis) -> object | None:
    """The operator's raw value for this axis, or ``None`` when unset."""
    if not isinstance(config, dict):
        return None
    key = axis.value
    if key in config:
        return config[key]
    top, sub = key.split(".", 1)
    parent = config.get(top)
    if isinstance(parent, dict) and sub in parent:
        return parent[sub]
    return None


_ZONE_ROOT_SPELLING = "<zone-root>"


def _parse_entries(raw: object) -> tuple[FileScopeEntry, ...]:
    """Parse an operator path list into typed entries (lenient, like the decl
    parser: unusable items are dropped rather than crashing a permissions
    primitive at load).

    #3925: the literal string ``"<zone-root>"`` — the SAME notation this
    module's own docstring already uses to describe :class:`ZoneRoot`'s
    resolved rendering (line 24 above) — parses to the symbol itself, not a
    :class:`LiteralPath` naming a directory that happens to be spelled that
    way. This is what lets an operator write ``file.write: ["<zone-root>"]``
    instead of the unrestricted ``allow`` form: a grant scoped to the SAME
    zone :data:`FILE_SCOPE_SCHEMA` already uses for the read axis's default,
    resolved per-environment (``ws_base_dir`` under chat/web, ``project_root``
    elsewhere) rather than a path baked in at config-write time."""
    if not isinstance(raw, list):
        raw = [raw]
    out: list[FileScopeEntry] = []
    for item in raw:
        if item == _ZONE_ROOT_SPELLING:
            out.append(ZoneRoot())
        elif isinstance(item, str) and item:
            out.append(LiteralPath(path=item))
        elif isinstance(item, dict):
            path = str(item.get("path", ""))
            if not path:
                continue
            scope = "recursive" if item.get("scope") != "just_path" else "just_path"
            out.append(LiteralPath(path=path, scope=scope))
    return tuple(out)


@dataclass(frozen=True)
class ResolvedFileScope:
    """The answer to "which paths does this axis cover", for one environment.

    Both the membership question (the gate) and the enumeration question (the
    advertisement / system prompt) are answered from the SAME entries, so the
    two cannot drift."""

    axis: FileScopeAxis
    entries: tuple[FileScopeEntry, ...]
    zone_root: Path | None
    is_denied: bool = False

    @property
    def is_empty(self) -> bool:
        """True when the axis covers nothing (``deny``, or an explicit ``[]``)."""
        return not self.entries

    def contains(self, path: str) -> bool:
        """True when ``path`` is inside this scope (the gate's question)."""
        return any(self._entry_contains(entry, path) for entry in self.entries)

    def _entry_contains(self, entry: FileScopeEntry, path: str) -> bool:
        if isinstance(entry, Anywhere):
            return True
        if isinstance(entry, ZoneRoot):
            return _in_default_read_zone(path, self.zone_root)
        if isinstance(entry, ZoneStateDir):
            return _in_default_write_zone(path, self.zone_root)
        return _literal_contains(entry, path, self.zone_root)

    @property
    def advertised_paths(self) -> tuple[str, ...]:
        """The scope rendered as concrete paths — what the model is told it may
        touch (router tool gating + the system prompt's ``## Files`` section)."""
        base = self.zone_root or Path.cwd()
        out: list[str] = []
        for entry in self.entries:
            if isinstance(entry, Anywhere):
                out.append("*")
            elif isinstance(entry, ZoneRoot):
                out.append(str(base))
            elif isinstance(entry, ZoneStateDir):
                out.append(str(base / ".reyn"))
            else:
                out.append(entry.path)
        return tuple(out)


def _literal_contains(entry: LiteralPath, path: str, zone_root: Path | None) -> bool:
    base = zone_root or Path.cwd()
    try:
        target = Path(path).expanduser()
        target = (base / target).resolve() if not target.is_absolute() else target.resolve()
        declared = Path(entry.path).expanduser()
        declared = (
            (base / declared).resolve() if not declared.is_absolute() else declared.resolve()
        )
    except OSError:
        return False
    if target == declared:
        return True
    if entry.scope != "recursive":
        return False
    try:
        target.relative_to(declared)
        return True
    except ValueError:
        return False


def resolve_file_scope(
    config_permissions: dict | None,
    axis: FileScopeAxis,
    *,
    zone_root: Path | None = None,
) -> ResolvedFileScope:
    """Resolve one axis's path set from ``config + environment``.

    THE function of #3458: config value in, resolved set out. The runtime gate,
    the advertisement, and any later subsystem all call this — there is no other
    way to obtain the set, so a fourth caller cannot invent a different answer.
    """
    if _config_word(config_permissions, axis, "deny"):
        return ResolvedFileScope(axis=axis, entries=(), zone_root=zone_root, is_denied=True)
    if _config_word(config_permissions, axis, "allow"):
        return ResolvedFileScope(axis=axis, entries=(Anywhere(),), zone_root=zone_root)
    raw = _config_raw_list(config_permissions, axis)
    if raw is None:
        return ResolvedFileScope(
            axis=axis, entries=FILE_SCOPE_SCHEMA[axis].default, zone_root=zone_root,
        )
    return ResolvedFileScope(axis=axis, entries=_parse_entries(raw), zone_root=zone_root)


@dataclass(frozen=True)
class FileScopes:
    """Both axes resolved for one environment — what a permission layer holds."""

    read: ResolvedFileScope
    write: ResolvedFileScope

    def for_axis(self, axis: FileScopeAxis) -> ResolvedFileScope:
        return self.read if axis is FileScopeAxis.READ else self.write

    def advertised(self) -> dict[str, list[str]]:
        """``{"read": [...], "write": [...]}`` — the advertisement's view."""
        return {
            "read": list(self.read.advertised_paths),
            "write": list(self.write.advertised_paths),
        }


def resolve_file_scopes(
    config_permissions: dict | None, *, zone_root: Path | None = None,
) -> FileScopes:
    """Resolve both axes at once (the shape permission layers consume)."""
    return FileScopes(
        read=resolve_file_scope(config_permissions, FileScopeAxis.READ, zone_root=zone_root),
        write=resolve_file_scope(config_permissions, FileScopeAxis.WRITE, zone_root=zone_root),
    )
