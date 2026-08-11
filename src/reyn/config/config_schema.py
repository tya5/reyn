"""Recursive ReynConfig schema introspection.

Provides :func:`walk_config_schema` which traverses the ``ReynConfig``
dataclass hierarchy and yields :class:`SchemaNode` objects — one per
dotted key.  Used by ``reyn config fields``, ``reyn config get``, and
``reyn config set`` so those commands track the *real* schema rather
than a hand-maintained list (the former ``CONFIG_FIELDS`` allowlist,
now removed).

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
    """Dotted key, e.g. ``safety.loop.max_router_calls_per_turn``."""

    type_repr: str
    """Human-readable type string, e.g. ``int``, ``str | None``."""

    default: Any
    """Default value, or :data:`MISSING` if there is no static default."""

    is_dict_leaf: bool = False
    """True when this key is a free-form dict (operator sets arbitrary sub-keys)."""

    desc: str = ""
    """Description from ``field(metadata={'desc': ...})``, or empty string."""

    field_type: Any = None
    """Unwrapped (Optional-stripped) field type for scalar leaves, e.g. ``int``,
    ``str``, or a ``Literal[...]`` — ``None`` for dict leaves. Lets callers
    introspect valid values (e.g. pick a non-default ``Literal`` member)."""


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


@_dataclass(frozen=True)
class RenamedKeyHint:
    """The reason an unknown config key isn't valid, when known.

    ``destination``: the new dotted key, when this is a PLAIN rename with
    no value transform — :func:`~reyn.interfaces.cli.commands.config._migrate`
    (``reyn config migrate``) auto-rewrites ONLY when this is not ``None``.
    ``note``: human-readable explanation always shown in a warning/report,
    regardless of whether ``destination`` is set.

    ``destination is not None`` is ``migrate``'s ENTIRE decision for
    whether to auto-rewrite an entry — lead-coder's block on #4190: the
    original design used a syntactic proxy (whitespace in the hint string)
    for this semantic distinction (destination vs. explanation), which a
    future T1-T6 entry author has no reliable way to know about (documented
    in one docstring, not enforced) and which fails safe in the WRONG
    direction (a hint that happens to contain no space gets silently
    auto-rewritten). Encoding it as a type makes the distinction
    unrepresentable-wrong instead of merely undocumented.

    A key whose rename carries a VALUE TRANSFORM (e.g.
    ``_RENAMED_SANDBOX_POLICY_KEYS``'s boolean-inversion renames) sets
    ``destination=None`` — ``migrate`` reports it for manual review rather
    than guessing at the transform.
    """

    note: str
    destination: "str | None" = None


#: #4174 T0: a renamed config key (dotted, any level) -> a :class:`RenamedKeyHint`,
#: so an unknown-key warning can NAME the destination ("`model:` moved to
#: `llm.model:`") rather than just say "unknown", and ``reyn config migrate``
#: can tell a safe plain rename from one it must not guess at.
#: Starts EMPTY — no renames have landed yet; T1-T6 (#4174) populate this
#: incrementally as each rename lands, in the SAME PR as the rename itself
#: (co-locate the rename with the hint that explains it, not a second
#: registry someone has to remember to update). Single source: both
#: :func:`unknown_config_keys` (what's currently valid) and this map (what
#: USED to be valid and where it went) are read wherever an unknown key
#: needs explaining — never hand-duplicated into a warning string. A key
#: absent from this map is NOT a rename — see :func:`unknown_config_keys`'s
#: own docstring for why that distinction is a first-class case, not an
#: empty-string placeholder.
_RENAMED_CONFIG_KEYS: "dict[str, RenamedKeyHint]" = {}


#: #4174 T0 (owner ruling: "don't special-case sandbox.policy" — one
#: unknown-key handling across every level, no exception): free-form dict
#: leaves in the ``ReynConfig`` schema (``is_dict_leaf=True`` — the
#: operator may write ANY sub-key, so the generic recursive walk in
#: :func:`unknown_config_keys` correctly stops there) can still have their
#: OWN internal vocabulary — ``sandbox.policy`` is the one example today
#: (#3823's ``_SANDBOX_POLICY_CONFIG_KEYS``, a DIFFERENT vocabulary than
#: ``SandboxPolicy``'s own field names, decoupled on purpose — see that
#: module). This registry lets such a leaf plug its own validator in
#: WITHOUT becoming a second unknown-key code path: the recursive walker
#: calls it and folds the result into the SAME return dict, same message
#: shape, same caller. Keyed by the leaf's dotted key.
_FREEFORM_LEAF_VALIDATORS: "dict[str, Any]" = {}


def register_freeform_leaf_validator(
    dotted_key: str, validator: "Any",
) -> None:
    """Plug a free-form dict leaf's own inner-vocabulary check into the
    shared #4174 T0 unknown-key walk.

    *validator* is ``Callable[[dict], dict[str, RenamedKeyHint | None]]`` —
    same return shape as :func:`unknown_config_keys` itself (``{sub_key:
    hint_or_None}``, keys relative to *dotted_key*'s own dict, NOT yet
    prefixed — :func:`unknown_config_keys` does the prefixing). A leaf
    whose renames always carry a value transform (e.g. sandbox.policy)
    returns every hint with ``destination=None`` — see
    :class:`RenamedKeyHint`. Registration, not a hardcoded dict literal
    here, so ``security.sandbox.policy`` (a leaf module ``config_schema.py``
    must not import — that would invert the module's own dependency
    direction) can register itself instead of this module reaching into
    it.
    """
    _FREEFORM_LEAF_VALIDATORS[dotted_key] = validator


def known_top_level_keys() -> frozenset[str]:
    """The full set of valid top-level ``ReynConfig`` keys, derived from the
    SAME live schema :func:`walk_config_schema` already provides to
    ``reyn config fields`` — #4174 T0's explicit requirement: no second
    source of truth for "what's a known key"."""
    return frozenset(node.key.split(".", 1)[0] for node in walk_config_schema())


def _schema_index() -> "tuple[frozenset[str], frozenset[str], frozenset[str]]":
    """Derive ``(namespace_keys, dict_leaf_keys, scalar_leaf_keys)`` from the
    SAME live :func:`walk_config_schema` list — the recursive walk's only
    input, so it can never drift from what ``reyn config fields`` shows.

    ``namespace_keys`` are dotted prefixes with children (a nested
    dataclass, e.g. ``"llm"``, ``"llm.router"``) — :func:`unknown_config_keys`
    recurses into a raw dict found there. ``dict_leaf_keys`` accept any
    sub-key (free-form, e.g. ``"mcp.servers"``) — the walk stops and
    accepts everything, except for a registered
    :func:`register_freeform_leaf_validator`. ``scalar_leaf_keys`` are
    exact, non-recursing valid keys.
    """
    nodes = walk_config_schema()
    scalar_leaves = frozenset(n.key for n in nodes if not n.is_dict_leaf)
    dict_leaves = frozenset(n.key for n in nodes if n.is_dict_leaf)
    namespaces: set[str] = set()
    for node in nodes:
        parts = node.key.split(".")
        for i in range(1, len(parts)):
            namespaces.add(".".join(parts[:i]))
    return frozenset(namespaces), dict_leaves, scalar_leaves


def unknown_config_keys(
    raw: "dict | None", *, prefix: str = "",
) -> "dict[str, RenamedKeyHint | None]":
    """Return ``{dotted_key: hint_or_None}`` for every key in *raw*
    (recursively, at every nesting level) that is not a valid
    ``ReynConfig`` field — #4174 T0's ONE unknown-key implementation,
    called from ``reyn config validate``, the config-load startup path,
    AND the hot-reload validator (:func:`reyn.runtime.hot_reload.validate_in_set`)
    — never separate hand-written checks per call site.

    Recurses into a namespace (a nested dataclass, e.g. ``llm.router``);
    stops at a free-form dict-leaf (e.g. ``mcp.servers`` — any sub-key is
    valid there) UNLESS that leaf has a
    :func:`register_freeform_leaf_validator` registered (``sandbox.policy``
    is the one example — #3823's own vocabulary, folded in rather than
    duplicated).

    A ``None`` hint means "this key matches none of the known keys — see
    ``reyn config fields``" (today's ONLY case: no rename has landed yet).
    A :class:`RenamedKeyHint` means the key was intentionally renamed (see
    :data:`_RENAMED_CONFIG_KEYS`) — its ``note`` is always shown; its
    ``destination`` tells ``reyn config migrate`` whether it's safe to
    auto-rewrite. Collects every unknown key in one pass — callers must NOT
    early-return on the first hit (owner requirement: report all problems
    at once, not one-fix-restart-repeat).
    """
    if not isinstance(raw, dict):
        return {}
    namespaces, dict_leaves, scalar_leaves = _schema_index()
    result: "dict[str, RenamedKeyHint | None]" = {}
    for key, value in raw.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if dotted in scalar_leaves:
            continue
        if dotted in dict_leaves:
            validator = _FREEFORM_LEAF_VALIDATORS.get(dotted)
            if validator is not None and isinstance(value, dict):
                for sub_key, hint in validator(value).items():
                    result[f"{dotted}.{sub_key}"] = hint
            continue
        if dotted in namespaces:
            if isinstance(value, dict):
                result.update(unknown_config_keys(value, prefix=dotted))
            continue
        result[dotted] = _RENAMED_CONFIG_KEYS.get(dotted)
    return result


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
            from reyn.runtime.external_routing import ExternalTransportRouting  # noqa: PLC0415
            ns["ExternalTransportRouting"] = ExternalTransportRouting
        except ImportError:
            pass
    if "OAuthProviderConfig" not in ns:
        try:
            from reyn.security.secrets.oauth import OAuthProviderConfig  # noqa: PLC0415
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
        if _is_schema_internal(dc_field):
            # Field is internal storage (e.g. loader-derived from a different
            # operator key), not an operator-settable schema key — omit it
            # from the settable schema entirely so config set/get/fields and
            # the doc-mirror guard never advertise a key the set/get path
            # can't honor. The field's value still flows to its runtime
            # consumers via the loader; it just isn't a settable dotted key.
            continue
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
                field_type=inner,
            ))


def _field_desc(f: dataclasses.Field) -> str:  # type: ignore[type-arg]
    """Extract ``desc`` from ``field(metadata={'desc': ...})``, or return ``""``."""
    meta = getattr(f, "metadata", None)
    if meta and isinstance(meta, typing.Mapping):
        return str(meta.get("desc", ""))
    return ""


def _is_schema_internal(f: dataclasses.Field) -> bool:  # type: ignore[type-arg]
    """True when a field opts out of the operator-settable schema.

    A field flags itself with ``field(metadata={'schema_internal': True})``
    when it is internal storage (e.g. loader-derived from a *different*
    operator key) rather than a directly settable config key. ``walk_config_schema``
    omits such fields so ``reyn config set/get/fields`` and the doc-mirror
    guard don't advertise a key whose set/get round-trip the loader can't honor.
    """
    meta = getattr(f, "metadata", None)
    if meta and isinstance(meta, typing.Mapping):
        return bool(meta.get("schema_internal", False))
    return False
