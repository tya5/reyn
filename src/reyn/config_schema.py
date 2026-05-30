"""Recursive ReynConfig schema introspection.

Provides :func:`walk_config_schema` which traverses the ``ReynConfig``
dataclass hierarchy and yields :class:`SchemaNode` objects — one per
dotted key.  Used by ``reyn config fields``, ``reyn config get``, and
``reyn config set`` so those commands track the *real* schema rather
than a hand-maintained ``CONFIG_FIELDS`` list.

Design notes
------------
- **Forward-ref robustness**: ``from __future__ import annotations``
  makes all type annotations strings.  Naively calling
  ``typing.get_type_hints(ReynConfig)`` fails on forward refs like
  ``'ExternalTransportRouting'`` that are lazily imported in
  ``config.py``.  We resolve per-dataclass using the *class module's*
  own ``__dict__`` as ``globalns``, extended with the two known
  lazy-import types.  See :func:`_get_hints_safe`.

- **Dict leaf (free-form)**: any field whose unwrapped type is ``dict``
  or ``dict[K, V]`` is treated as a free-form dict — the operator may
  set arbitrary sub-keys under it (e.g. ``mcp.servers.github.url``).
  We emit a single :class:`SchemaNode` with ``is_dict_leaf=True``
  instead of recursing.

- **Scalar leaf**: any field whose unwrapped type is not a dataclass
  and not a dict is a scalar leaf.

- **None-vs-unknown**: the walk records the real default value
  (including ``None``) so callers can distinguish "key exists, default
  is None" from "key does not exist in schema".
"""
from __future__ import annotations

import dataclasses
import sys
import types
import typing
from dataclasses import dataclass as _dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@_dataclass
class SchemaNode:
    """One entry in the flattened config schema."""

    key: str
    """Dotted key, e.g. ``safety.loop.max_phase_visits``."""

    type_repr: str
    """Human-readable type string, e.g. ``int``, ``str | None``."""

    default: Any
    """Default value, or :data:`MISSING` if there is no static default."""

    is_dict_leaf: bool = False
    """True when this key is a free-form dict (operator sets arbitrary sub-keys)."""

    desc: str = ""
    """Description from ``field(metadata={'desc': ...})``, or empty string."""


#: Sentinel for fields whose default can only be computed by calling
#: ``default_factory()``.  We eagerly call the factory so this sentinel
#: should only appear if the factory itself raises.
MISSING: object = dataclasses.MISSING


def walk_config_schema(cls: type | None = None) -> list[SchemaNode]:
    """Return a flat list of :class:`SchemaNode` for every dotted key in *cls*.

    *cls* defaults to :class:`~reyn.config.ReynConfig`.  Call with a
    sub-dataclass to walk a sub-tree.

    The list order is depth-first, reflecting field declaration order.
    """
    if cls is None:
        from reyn.config import ReynConfig  # noqa: PLC0415
        cls = ReynConfig
    nodes: list[SchemaNode] = []
    _walk(cls, prefix="", nodes=nodes, seen=set())
    return nodes


def is_valid_config_key(key: str) -> bool:
    """Return True when *key* is a valid config key.

    A key is valid if:
    - It exactly matches a leaf node's dotted key, OR
    - It starts with a dict-leaf node's dotted key followed by a ``"."``
      (free-form sub-key, e.g. ``mcp.servers.github.url``).
    """
    nodes = walk_config_schema()
    for node in nodes:
        if node.is_dict_leaf:
            if key == node.key or key.startswith(node.key + "."):
                return True
        else:
            if key == node.key:
                return True
    return False


def resolve_config_value(config: Any, key: str) -> tuple[bool, Any]:
    """Resolve a dotted *key* against a loaded config instance.

    Returns ``(found, value)`` where *found* is True when the key exists
    in the schema.  When *found* is False, *value* is ``None``.  When
    *found* is True, *value* may legitimately be ``None`` — callers
    **must** use the boolean, not a None-check, to distinguish "unknown
    key" from "key with None value".
    """
    if not is_valid_config_key(key):
        return False, None

    parts = key.split(".")
    obj: Any = config
    for part in parts:
        if obj is None:
            return True, None
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            if hasattr(obj, part):
                obj = getattr(obj, part)
            else:
                # Free-form dict key that doesn't exist yet — valid key, absent value.
                return True, None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            # Scalar reached before path was exhausted — treat as absent.
            return True, None
    return True, obj


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_hints_safe(cls: type) -> dict[str, Any]:
    """Call ``typing.get_type_hints`` robustly for *cls*.

    Uses the class's own module namespace as ``globalns`` to resolve
    forward refs declared in that module.  Adds the two known
    lazy-imported types (``ExternalTransportRouting``,
    ``OAuthProviderConfig``) which are not present in ``reyn.config``'s
    module globals at import time.

    Returns an empty dict on failure (= walk falls back to skipping).
    """
    try:
        localns: dict[str, Any] = dict(vars(sys.modules[cls.__module__]))
        # Patch in lazy-import types that are referenced as forward refs
        # but not imported at the top of config.py.
        _patch_localns(localns)
        return typing.get_type_hints(cls, globalns=localns)
    except Exception:
        # Fallback: return empty so the walk skips this class cleanly
        # rather than crashing.  Callers will log / skip silently.
        return {}


def _patch_localns(ns: dict[str, Any]) -> None:
    """Inject lazy-import types that appear as forward refs in config.py."""
    if "ExternalTransportRouting" not in ns:
        try:
            from reyn.chat.external_routing import ExternalTransportRouting  # noqa: PLC0415
            ns["ExternalTransportRouting"] = ExternalTransportRouting
        except ImportError:
            pass
    if "OAuthProviderConfig" not in ns:
        try:
            from reyn.secrets.oauth import OAuthProviderConfig  # noqa: PLC0415
            ns["OAuthProviderConfig"] = OAuthProviderConfig
        except ImportError:
            pass


def _unwrap_optional(ftype: Any) -> Any:
    """Strip ``Optional`` / ``X | None`` wrappers.

    Returns the unwrapped type if the only non-None arg exists,
    otherwise returns *ftype* unchanged.
    """
    if isinstance(ftype, types.UnionType):
        args = typing.get_args(ftype)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
        return ftype
    origin = getattr(ftype, "__origin__", None)
    if origin is typing.Union:
        args = typing.get_args(ftype)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return ftype


def _is_dict_type(ftype: Any) -> bool:
    """Return True when *ftype* is any flavour of ``dict``."""
    if ftype is dict:
        return True
    origin = getattr(ftype, "__origin__", None)
    return origin is dict


def _type_repr(ftype: Any) -> str:
    """Return a short human-readable string for *ftype*."""
    if hasattr(ftype, "__name__"):
        return ftype.__name__
    return str(ftype)


def _field_default(f: dataclasses.Field) -> Any:  # type: ignore[type-arg]
    """Return the best available default for a dataclass field.

    Calls ``default_factory()`` when there is no static default.
    Returns :data:`MISSING` only when both are absent.
    """
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        try:
            return f.default_factory()  # type: ignore[misc]
        except Exception:
            return MISSING
    return MISSING


def _walk(
    cls: type,
    prefix: str,
    nodes: list[SchemaNode],
    seen: set[type],
) -> None:
    """Depth-first traversal of *cls* (a dataclass).

    Mutates *nodes* in place.  *seen* prevents infinite recursion on
    self-referential schemas (shouldn't occur in practice but guards
    against it).
    """
    if cls in seen:
        return
    seen = seen | {cls}

    if not dataclasses.is_dataclass(cls):
        return

    hints = _get_hints_safe(cls)
    fields_map: dict[str, dataclasses.Field] = {  # type: ignore[type-arg]
        f.name: f for f in dataclasses.fields(cls)
    }

    for fname, ftype in hints.items():
        if fname not in fields_map:
            continue
        dc_field = fields_map[fname]
        dotkey = f"{prefix}.{fname}" if prefix else fname
        inner = _unwrap_optional(ftype)

        if _is_dict_type(inner):
            # Free-form dict leaf — operator may set arbitrary sub-keys.
            desc = _field_desc(dc_field)
            default = _field_default(dc_field)
            nodes.append(SchemaNode(
                key=dotkey,
                type_repr="dict",
                default=default,
                is_dict_leaf=True,
                desc=desc,
            ))
        elif dataclasses.is_dataclass(inner):
            # Nested dataclass — recurse.
            _walk(inner, prefix=dotkey, nodes=nodes, seen=seen)
        else:
            # Scalar leaf.
            desc = _field_desc(dc_field)
            default = _field_default(dc_field)
            nodes.append(SchemaNode(
                key=dotkey,
                type_repr=_type_repr(inner),
                default=default,
                is_dict_leaf=False,
                desc=desc,
            ))


def _field_desc(f: dataclasses.Field) -> str:  # type: ignore[type-arg]
    """Extract ``desc`` from ``field(metadata={'desc': ...})``, or return ``""``."""
    meta = getattr(f, "metadata", None)
    if meta and isinstance(meta, typing.Mapping):
        return str(meta.get("desc", ""))
    return ""
