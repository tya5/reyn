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
  makes all type annotations strings.  ``typing.get_type_hints(ReynConfig)``
  resolves each forward ref against the *class module's* own ``__dict__``
  as ``globalns`` — every dataclass field type referenced only as a string
  (``ExternalTransportRouting``, ``OAuthProviderConfig``, ...) MUST be a
  concrete, non-``TYPE_CHECKING`` import in that module or the field
  silently drops from the schema (#4653).  See :func:`_get_hints_safe`.

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


@_dataclass(frozen=True)
class RemovedKeyHint:
    """The reason an unknown config key isn't valid, when it was DELETED
    with no successor — a distinct category from :class:`RenamedKeyHint`
    (#4375, lead-coder's ruling ①).

    ``_RENAMED_CONFIG_KEYS`` is a table of "where did this move to" —
    every entry there has, or could have, a destination. A key that was
    genuinely removed has none, and putting it in that table either
    leaves no way to write an honest hint (no destination to name) or
    invites writing a false one. The operator's next action differs by
    kind, not just degree: a rename says "rewrite this to Y"; a removal
    says "delete this, there is no Y" — encoding it as a separate type
    makes that difference structural instead of something a hint's
    author has to remember to phrase correctly each time (same reasoning
    :class:`RenamedKeyHint`'s own ``destination`` field already applies
    to the rewrite-vs-manual-review distinction).

    Deliberately carries no ``destination`` field at all — unlike
    :class:`RenamedKeyHint` where ``destination=None`` is a valid state
    (a value-transform rename, manual review only), a removed key MUST
    stay unrewritable; making the field simply not exist here makes that
    unrepresentable-wrong rather than merely a convention.
    """

    note: str


#: #4375: top-level ``ReynConfig`` keys that existed at some point in the
#: schema's own history and were deleted with NO successor key — a
#: DIFFERENT population than :data:`_RENAMED_CONFIG_KEYS` (which only
#: covers keys that MOVED). Derived by architect's #4375 measurement: a
#: union of every top-level field name across all ~148 historical
#: revisions of the schema's own module (``git log --follow``), minus the
#: 33 that are still current, minus the 8 that are registered renames
#: above — 17 remain, confirmed real (not a false-positive population)
#: because #4373's two hand-found stale keys (``shell_allowed`` /
#: ``skill_search``) are both in this list. Individual per-key removal
#: provenance (which PR deleted each one, and why) was NOT traced — #4375
#: measured the SCHEMA'S OWN population, not each key's history, and an
#: operator needs "delete this, it's gone" more than a specific PR
#: citation. Same co-location discipline as ``_RENAMED_CONFIG_KEYS``: a
#: future removal adds its entry here in the SAME PR as the removal.
_REMOVED_CONFIG_KEYS: "dict[str, RemovedKeyHint]" = {
    key: RemovedKeyHint(
        note=f"`{key}:` no longer exists in the schema and has no "
             "replacement key (traced via #4375: a union of every "
             "top-level key across the config schema's own revision "
             "history, 58 that ever existed vs. 33 current) — delete "
             "it from your config."
    )
    for key in (
        "eval",
        "limits",
        "max_phase_visits",
        "mcp_search_threshold",
        "multi_agent",
        "plan",
        "plan_resume_raw",
        "python",
        "routerloop_convergence_skills",
        "self_improvement",
        "shell_allowed",
        "skill_resume",
        "skill_search",
        "state_dir",
        "time_travel",
        "tool_calls_op_loop_skills",
        "workspace",
    )
}


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
_RENAMED_CONFIG_KEYS: "dict[str, RenamedKeyHint]" = {
    # #4174 T5. A plain rename — same nested shape (`max_bytes` /
    # `max_age_seconds` / `cleanup_period_days` unchanged), only the
    # top-level key moved — so `destination` is set and `reyn config
    # migrate` auto-rewrites it.
    "events": RenamedKeyHint(
        note="`events:` moved to `audit_events:` (bare \"event\" is ambiguous "
             "— reyn's own \"event\" spans audit-event / WAL-event / "
             "hook-event; this block was always audit-event only)",
        destination="audit_events",
    ),
    # #4174 T5. NOT a plain rename — `agent: {id: X}` (a namespace) became
    # a top-level scalar `agent_id: X` (a value TRANSFORM, dict-to-scalar),
    # so `destination` stays None: `reyn config migrate` reports this for
    # manual review rather than guessing at the unwrap.
    "agent": RenamedKeyHint(
        note="`agent:` (a namespace wrapping a single `id` field) moved to "
             "the top-level scalar `agent_id:` — move the `id:` value up "
             "one level and rename the key, e.g. `agent: {id: X}` -> "
             "`agent_id: X`",
    ),
    # #4174 T4. NOT a plain rename — `web:` conflated two unrelated
    # subsystems (the web_fetch TOOL's TLS settings, `web.fetch`, and the
    # `reyn web` GATEWAY's own settings, `web.auth`/`web.ws_max_size`/
    # `web.surfaces`) that now split to TWO different destinations
    # (`web_fetch:` / `gateway:`). A single old key mapping to more than
    # one new key cannot be expressed as one `destination` — `migrate`
    # cannot guess which sub-keys the operator's block actually has, so
    # `destination` stays None and this reports for manual review, same
    # category as `agent` above. (The unknown-key walk also never
    # recurses PAST an already-unknown top-level key — see
    # `unknown_config_keys`'s own docstring — so a per-sub-key entry like
    # `"web.fetch"` would never be reached anyway; one entry on `"web"`
    # is the only reachable shape here.)
    "web": RenamedKeyHint(
        note="`web:` split into two keys: move `web.fetch.*` to the "
             "top-level `web_fetch.*` (unchanged fields — verify_ssl / "
             "ca_bundle / max_download_bytes / allow_private_ips), and "
             "move `web.auth` / `web.ws_max_size` / `web.surfaces` / "
             "`web.default_design` under a new top-level `gateway:` "
             "block (same field names, just renested there — #4317 "
             "added `default_design`, dropped by the original T4 split)",
    ),
    # #4174 T3. All 5 are plain renames — same shape, only the top-level key
    # moved under `llm:` (which already existed for router/retry) — so
    # `destination` is set on each and `reyn config migrate` auto-rewrites
    # them. `llm:` itself is a known key (LLMConfig), so these entries are
    # only reached when the OLD top-level key is used — `unknown_config_keys`
    # never recurses past a key it already knows, so `llm.model` etc. being
    # valid doesn't shadow `model` still being flagged at the top level.
    "model": RenamedKeyHint(
        note="`model:` moved to `llm.model:` (LLM-domain settings — "
             "model selection now lives alongside `llm.router`/`llm.retry`)",
        destination="llm.model",
    ),
    "models": RenamedKeyHint(
        note="`models:` moved to `llm.models:` (same shape — map of model "
             "class name to LiteLLM model string)",
        destination="llm.models",
    ),
    "model_class_by_purpose": RenamedKeyHint(
        note="`model_class_by_purpose:` moved to "
             "`llm.model_class_by_purpose:` (same shape — purpose → model "
             "class)",
        destination="llm.model_class_by_purpose",
    ),
    "api_base": RenamedKeyHint(
        note="`api_base:` moved to `llm.api_base:` (LiteLLM proxy base URL)",
        destination="llm.api_base",
    ),
    "prompt_cache_enabled": RenamedKeyHint(
        note="`prompt_cache_enabled:` moved to `llm.prompt_cache_enabled:` "
             "(same bool)",
        destination="llm.prompt_cache_enabled",
    ),
}


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


#: #4655: the second, EXPLICIT registration kind a free-form dict-leaf can
#: take — "we looked at this leaf's real consumer(s) and deliberately do
#: NOT check its inner vocabulary" (genuinely open, operator-chosen names:
#: model names, provider names, header names, ...). This is deliberately a
#: SEPARATE registry from :data:`_FREEFORM_LEAF_VALIDATORS`, not a
#: validator that always returns ``{}`` — a leaf simply absent from BOTH
#: registries is indistinguishable from an oversight (nobody ever looked
#: at it), which is the exact defect #4655 exists to catch. Every dict-leaf
#: must end up in EXACTLY ONE of the two registries — see
#: :func:`unregistered_freeform_leaves`.
_FREEFORM_LEAF_DECLARED_OPEN: "set[str]" = set()


def register_freeform_leaf_open(dotted_key: str) -> None:
    """Explicitly declare a free-form dict-leaf's inner vocabulary as
    Kind ② — deliberately NOT checked (#4655).

    Use this instead of :func:`register_freeform_leaf_validator` when a
    leaf's real consumer(s) read its sub-keys generically (``.get(name)``/
    ``.items()`` with a genuinely operator-chosen name — a model name, a
    provider name, an HTTP header name, ...) so there is no bounded finite
    vocabulary to check. Recording the decision here — rather than simply
    never calling anything for *dotted_key* — is the whole point: an
    unregistered leaf and a deliberately-open leaf both currently accept
    every sub-key silently, but only one of them was actually LOOKED AT.
    See :func:`unregistered_freeform_leaves`, the completeness check that
    reads this registry to tell the two apart.
    """
    _FREEFORM_LEAF_DECLARED_OPEN.add(dotted_key)


def freeform_leaf_registration_kind(dotted_key: str) -> "str | None":
    """Public read of a free-form dict-leaf's #4655 registration —
    ``"validated"`` (:func:`register_freeform_leaf_validator`, Kind①),
    ``"open"`` (:func:`register_freeform_leaf_open`, Kind②), or ``None``
    (neither — registered under neither kind).

    Exists so a caller (a test, ``reyn config fields``, ...) can ask "how
    is this leaf disposed of" without reaching into
    :data:`_FREEFORM_LEAF_VALIDATORS` / :data:`_FREEFORM_LEAF_DECLARED_OPEN`
    directly — those two dicts/sets are the mechanism's storage, this
    function is its read surface.
    """
    if dotted_key in _FREEFORM_LEAF_VALIDATORS:
        return "validated"
    if dotted_key in _FREEFORM_LEAF_DECLARED_OPEN:
        return "open"
    return None


def unregistered_freeform_leaves() -> "frozenset[str]":
    """#4655 completeness check: every ``is_dict_leaf`` key from the SAME
    live :func:`walk_config_schema` this module already uses elsewhere
    (:func:`_schema_index`, :func:`known_top_level_keys`) that has NEITHER
    a :func:`register_freeform_leaf_validator` (Kind ①) NOR a
    :func:`register_freeform_leaf_open` (Kind ②) registration.

    A non-empty result means a dict-leaf field was added to ``ReynConfig``
    (or gained ``is_dict_leaf=True`` via the ``dict_leaf`` metadata escape
    hatch) without anyone deciding what its inner vocabulary check should
    be — same shape as #1983 (``Op`` union ↔ ``OP_KIND_MODEL_MAP``) and
    #4646 (parser step-kinds ↔ ``executor.STEP_KINDS``): a derived pair,
    checked for drift, rather than a hand-maintained list nobody
    re-verifies. ``tests/core/test_4655_freeform_leaf_registration_completeness.py``
    asserts this is empty — every registration module that calls either
    registration function must be IMPORTED before that assertion runs, or
    its leaves report as "unregistered" false-positively (decorators /
    module-level calls only execute once the module is imported).
    """
    _namespaces, dict_leaves, _scalar_leaves = _schema_index()
    return frozenset(
        dict_leaves
        - _FREEFORM_LEAF_VALIDATORS.keys()
        - _FREEFORM_LEAF_DECLARED_OPEN
    )


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
) -> "dict[str, RenamedKeyHint | RemovedKeyHint | None]":
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

    **#4515 incident**: recursing into a namespace assumes the RAW config
    shape mirrors the DATACLASS shape exactly, one nesting level per
    field. That broke for ``external_transports`` — its field type
    (``ExternalTransportRouting``) wraps a single ``transports: dict``
    field for the wrapper's own ``.get(name)`` convenience, but the real
    reyn.yaml has no ``transports:`` key at all (the operator writes
    ``external_transports: {broker: {...}}`` directly). The walk
    registered the dict-leaf one level too deep
    (``external_transports.transports``), so EVERY real transport name
    matched none of ``namespaces``/``dict_leaves``/``scalar_leaves`` and
    was reported "not recognized ... NOT APPLIED" — while
    ``loader.py``'s ``_build_external_transports_config`` was, in fact,
    applying it correctly the whole time. A false "not applied" is worse
    than no warning: an operator reads it as "this setting does
    nothing" and deletes working config (architect hit this directly
    setting up reyn-gpt, #4501). Fixed via ``field(metadata={'dict_leaf':
    True})`` (see :func:`_is_dict_leaf_override`) — a reusable escape
    hatch for the whole SHAPE class (a wrapper dataclass around one dict
    field whose own name never appears in real config), not a
    single-field patch.

    A ``None`` hint means "this key matches none of the known keys — see
    ``reyn config fields``" (a typo, or a key that never existed at all).
    A :class:`RenamedKeyHint` means the key MOVED (see
    :data:`_RENAMED_CONFIG_KEYS`) — its ``note`` is always shown; its
    ``destination`` tells ``reyn config migrate`` whether it's safe to
    auto-rewrite. A :class:`RemovedKeyHint` (#4375) means the key was
    DELETED with no successor (see :data:`_REMOVED_CONFIG_KEYS`) — its
    ``note`` says so; it carries no ``destination`` because there is
    nowhere to rewrite it TO. Both hint types expose ``.note``, so a
    caller that only prints the note (every current call site) needs no
    ``isinstance`` branch; a caller that acts on ``destination`` (``reyn
    config migrate``) already only ever reads ``_RENAMED_CONFIG_KEYS``
    directly for that, never this function's combined result, so a
    ``RemovedKeyHint`` reaching that code is not a case it has to guard
    against. Collects every unknown key in one pass — callers must NOT
    early-return on the first hit (owner requirement: report all problems
    at once, not one-fix-restart-repeat).
    """
    if not isinstance(raw, dict):
        return {}
    namespaces, dict_leaves, scalar_leaves = _schema_index()
    result: "dict[str, RenamedKeyHint | RemovedKeyHint | None]" = {}
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
        result[dotted] = (
            _RENAMED_CONFIG_KEYS.get(dotted) or _REMOVED_CONFIG_KEYS.get(dotted)
        )
    return result


# ---------------------------------------------------------------------------
# #4231 (C): known keys DISABLED by another key's current value
# ---------------------------------------------------------------------------


@_dataclass(frozen=True)
class DisabledByHint:
    """A KNOWN, correctly-spelled config key whose value has no effect
    under the CURRENT value of another key it depends on.

    A DIFFERENT category than :class:`RenamedKeyHint` / :func:`unknown_config_keys`
    (#4174 T0 handles unknown keys — a typo, or a key that never existed).
    This one is for a key that IS real, IS spelled correctly, and is
    genuinely read somewhere in the codebase — architect's #4231 ruling:
    the discriminator for "does this deserve a check, or deletion" is
    "is there at least one read path" (``universal_wrappers_enabled`` has
    one — the ``universal-category`` scheme genuinely consumes it — vs.
    #3907/#3962's fields that NO code ever read, which were deleted, not
    documented-as-inert). A key that would ever fall into THIS category
    but has zero read paths anywhere belongs back in the delete-it
    discipline, not here.

    Same 4-element warning discipline #4174 T0's own report already
    established (architect, #4231): state the result plainly, name the
    conflicting key (and ideally its current value), batch every finding
    in one pass (never early-return), and give an actionable fix.
    """

    #: Plain statement of the result — "has no effect under the current
    #: tool_use.scheme" — always shown.
    note: str
    #: The OTHER key this one's applicability depends on, dotted
    #: (``"tool_use.scheme"``) — so the operator knows what to look at.
    dependency_key: str
    #: One concrete remedy — change the dependency, or drop the disabled
    #: key. Never "this has no effect" alone (the #4179 lesson: name what
    #: to do next, don't just report a state).
    fix: str


def _check_universal_wrappers_enabled_scheme_mismatch(
    raw: dict,
) -> "DisabledByHint | None":
    """#4231: does ``tool_use.universal_wrappers_enabled: true`` reach
    anything a non-``universal-category`` ``tool_use.scheme`` would
    render? Answered by DESTINATION, not by "who reads the flag" (#4564's
    lesson: a discriminator grounded in the reader missed a real effect
    the flag had via a DERIVED value, not a direct read — see below).

    #4552 PR-3: this check is RELOCATED here, not superseded — the field
    itself moved from ``action_retrieval.universal_wrappers_enabled`` to
    ``tool_use.universal_wrappers_enabled`` (architect's ruling: it is a
    tool_use/presentation-scheme property, not a retrieval setting), so
    both values this check compares now live under the SAME top-level
    ``tool_use:`` key. The underlying inconsistency #4231 named is
    otherwise UNCHANGED by the move — #4564 fixed a real but SEPARATE
    defect (the flag's undeclared reach into ``search_actions``
    visibility), not this scheme-mismatch itself, which #4564 did not
    touch and remains real.

    Where the flag's effect actually lands, as of #4564:

    - The ``universal-category`` scheme's OWN 3 non-search wrapper
      functions (``list_actions`` / ``describe_action`` / ``invoke_action``)
      — real, scheme-scoped, confirmed via ``RouterHostAdapter.
      get_universal_wrappers_enabled()`` → ``_category_exposure.
      build_category_exposure`` (imported by ``universal_category.py``'s
      scheme, and nowhere else).
    - That SAME scheme's own ``search_actions_enabled`` SP-text claim
      (``_category_exposure.py``: ``sv if univ else True``) — also real,
      also scheme-scoped.
    - Nothing else. Before #4564, the flag ALSO gated whether
      ``search_actions`` was VISIBLE AT ALL, for every scheme — not
      through a read of ``get_universal_wrappers_enabled()`` inside any
      scheme file, but because ``router_loop.py``'s OS-level
      ``_search_visible`` computation (the shared D14 gate every scheme's
      ``present()`` call reads via ``layer_ctx["search_visible"]``) sat
      inside an ``if _univ_enabled:`` block that was never part of
      ``search_actions``'s declared contract (``embedding.py``: "gated
      separately via ``embedding.enabled``"). #4564 removed that
      undeclared gate — the flag's reach is now exactly the two bullets
      above, matching this check's original #4231 premise for the first
      time.

    Re-measured before EXTENDING this check for #4564 (architect's own
    explicit caveat on the #4231 ruling, reapplied — "re-measure before
    implementing, don't build a mechanism on an assumption"): a
    flag=False-side check was drafted and discarded once the code fix
    landed, because after #4564 the flag has ZERO effect on
    ``search_actions`` in any scheme — there is nothing left for a
    False-side arm to detect.

    Fires ONLY when the raw file EXPLICITLY sets the flag to ``True``
    (the resolved *default* is also ``True`` — firing on the unset
    default would warn nearly every operator who never touched this key
    at all, which is not what "explicit" means in architect's ruling).
    Deliberately kept SOFT (warn via ``reyn config validate``, never
    raised) — an explicit, standing owner ruling governs config
    validation uniformly ("warn, never hard-fail, anywhere — including
    sandbox.policy, no special case", ``loader.py``'s
    ``_warn_unknown_config_keys`` docstring), overriding
    ``ToolUseConfig``'s own local convention of raising for its sibling
    fields (see that class's docstring for the same note from the other
    side)."""
    tool_use = raw.get("tool_use")
    if not isinstance(tool_use, dict):
        return None
    if tool_use.get("universal_wrappers_enabled") is not True:
        return None
    scheme = tool_use.get("scheme")
    if scheme is None:
        scheme = "enumerate-all"  # the #1657 owner default (execution.py's own)
    if scheme == "universal-category":
        return None
    return DisabledByHint(
        note=(
            f"tool_use.universal_wrappers_enabled: true has no effect "
            f"under tool_use.scheme: {scheme!r} — only the 'universal-category' "
            f"scheme uses universal wrappers; {scheme!r} has its own, "
            f"mutually-exclusive presentation mechanism."
        ),
        dependency_key="tool_use.scheme",
        fix=(
            "set tool_use.scheme: universal-category to make this flag apply, "
            "or remove tool_use.universal_wrappers_enabled if you did "
            "not intend to opt into the universal-category scheme."
        ),
    )


#: #4231 (C): registered (key -> check) pairs, one per known
#: dependency-disabled config knob. Deliberately a small, explicit
#: registry rather than a general "declare key dependencies" framework —
#: architect's own ruling scoped that broader mechanism to a SEPARATE,
#: larger issue (other same-shaped knobs likely exist — embedding.* under
#: embedding.enabled=false, offload.* under offload.enabled=false — but
#: are UNENUMERATED; adding one here does not imply they're covered).
_DISABLED_KEY_CHECKS: "dict[str, Any]" = {
    # #4552 PR-3: relocated from "action_retrieval.universal_wrappers_enabled"
    # — the field itself moved; the dict key names where it lives NOW.
    "tool_use.universal_wrappers_enabled": (
        _check_universal_wrappers_enabled_scheme_mismatch
    ),
}


def disabled_config_keys(raw: "dict | None") -> "dict[str, DisabledByHint]":
    """Return ``{dotted_key: DisabledByHint}`` for every KNOWN config key in
    *raw* whose value is currently inert because of another key's value —
    #4231 (C)'s "make the inconsistency speak" mechanism, the sibling of
    :func:`unknown_config_keys` (#4174 T0) for a DIFFERENT defect class
    (a real key, silently ignored under the current configuration, vs. a
    key that was never real at all).

    Collects every finding in one pass (never early-returns) — the SAME
    "report all problems at once" discipline :func:`unknown_config_keys`
    already established."""
    if not isinstance(raw, dict):
        return {}
    result: "dict[str, DisabledByHint]" = {}
    for key, check in _DISABLED_KEY_CHECKS.items():
        hint = check(raw)
        if hint is not None:
            result[key] = hint
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_hints_safe(cls: type) -> dict[str, Any]:
    """Call ``typing.get_type_hints`` robustly for *cls*.

    Uses the class's own module namespace as ``globalns`` to resolve
    forward refs declared in that module.  Every forward-ref field type
    (``ExternalTransportRouting``, ``OAuthProviderConfig``, ...) must
    already be a concrete import in that module (#4653) — this no longer
    patches in a hardcoded name list; a missing import surfaces as a
    ruff F821 finding at the declaration site instead of a silent schema
    drop here.

    Returns an empty dict on failure (= walk falls back to skipping).
    """
    try:
        localns: dict[str, Any] = dict(vars(sys.modules[cls.__module__]))
        return typing.get_type_hints(cls, globalns=localns)
    except Exception:
        # Fallback: return empty so the walk skips this class cleanly
        # rather than crashing.  Callers will log / skip silently.
        return {}


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

        if _is_dict_type(inner) or _is_dict_leaf_override(dc_field):
            # Free-form dict leaf — operator may set arbitrary sub-keys.
            # #4515: `_is_dict_leaf_override` covers a field whose TYPE is a
            # dataclass wrapper around exactly one dict field (e.g.
            # `ExternalTransportRouting(transports: dict)`) but whose REAL
            # reyn.yaml shape has no nested wrapper key — the operator writes
            # `external_transports: {broker: {...}}` directly, never
            # `external_transports: {transports: {broker: {...}}}`. Without
            # this override the recursion below registers the dict-leaf as
            # `external_transports.transports` (matching the DATACLASS, not
            # the real config), so every real transport name looks unknown —
            # a false "NOT APPLIED" warning on config that IS applied (see
            # `unknown_config_keys`'s own docstring for the full incident).
            # Emitted at THIS field's own dotkey (the wrapper), never the
            # inner field's — the wrapper class itself stays unchanged (its
            # `.transports`/`.get()` convenience API is a separate concern
            # from what the operator-facing schema shape is).
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


def _is_dict_leaf_override(f: dataclasses.Field) -> bool:  # type: ignore[type-arg]
    """True when a dataclass-typed field should be schema-classified as a
    free-form dict leaf (#4515) rather than recursed into as a namespace.

    A field flags itself with ``field(metadata={'dict_leaf': True})`` when
    its TYPE is a wrapper dataclass around exactly one dict field but its
    REAL reyn.yaml shape has no nested key matching that wrapped field's
    name — the wrapper exists for the TYPE's own consumers (a
    ``.get(name)`` convenience method, e.g.), not because the operator
    ever writes that extra nesting level. Without this override,
    ``_walk`` would recurse into the wrapper and register the dict-leaf
    one level too deep, so every real operator-written sub-key falsely
    reads as unknown (see :func:`unknown_config_keys`'s own docstring for
    the #4515 incident this fixes)."""
    meta = getattr(f, "metadata", None)
    if meta and isinstance(meta, typing.Mapping):
        return bool(meta.get("dict_leaf", False))
    return False
