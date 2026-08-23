"""#4206 slice 1 — declared reload classes for per-agent-profile keys.

Architect's own diagnosis (issuecomment-5384833640, after 2 rounds of
self-falsification on this exact question): a hand-maintained (key ×
layer) doc TABLE for "how often does an edit to this value take effect"
rots the same way `docs/reference/config/reyn-yaml.md`'s own
`Declared in` column did before #4206's own gate (`tests/repo/
test_config_reference_declared_in_4206.py`) — "the same event twice"
(#5166 / #5175 / #5197 that same night). The fix is the same shape those
3 landed gates already use: a key DECLARES its own reload class, once,
here; the doc's own section is a projection of this dict, not a second
hand-written source that can drift from it.

Scope, explicit (architect's own slice cut): ONLY the keys an agent's own
``.reyn/agents/<name>/profile.yaml`` can set — the surface an operator
actually touches when hand-authoring an agent (the #4206 issue's own
founding motivation: bringing up 2 coder agents by declaration alone).
Every OTHER config key in `reyn.yaml`'s own reference table stays
"未移行" (not yet migrated) — this slice does not attempt project-layer
or session-layer keys.

## TESTS-READ A finding, folded in (architect, issuecomment-5384894847)

**Block ① (fixed):** the first draft's own docstring claimed the whole
vocabulary was "derived, never hand-duplicated", but only the
``preferences``/``bounding`` legs actually were — the 4
:class:`~reyn.runtime.profile.AgentProfile`-scoped keys
(``role``/``allowed_mcp``/``base_dir``/``project_context_path``) were a
hand-typed literal set. A field added to ``AgentProfile`` would have
silently reached neither this registry NOR the completeness gate's own
notion of "the real vocabulary" — the exact "declared but the
completeness check can't see it" hole this whole slice exists to close,
reproduced one level up in its own implementation.
:func:`declared_agent_profile_keys` now derives that leg from
:func:`dataclasses.fields` directly, excluding only the 4 names that are
NOT independent reload-class questions
(``name``/``created_at``: identity/audit metadata; ``preferences``/
``bounding``: expanded into their own dotted keys separately, not
top-level keys in their own right) — with a witness test confirming each
excluded name is a REAL field on ``AgentProfile`` (not a bare set-equality
assert against the same literal on both sides, which would prove nothing
per CLAUDE.md's 6-questions #2).

**Block ② (fixed):** the first draft reused the project-layer's own
``hot`` word for ``allowed_mcp``, reasoning it was "the same explicit-
trigger-then-turn-boundary shape". Architect's own fresh measurement
(``origin/main``, ``src/``) falsified the analogy: ``profile.yaml`` is
NOT in ``_HOT_RELOAD_FILES`` — the project-layer ``hot`` class's actual
defining property is that **the file WRITE ITSELF is the trigger** (the
fs-watch-driven ``.reyn/config/``-side write is what causes the next-
turn-boundary re-read, per this doc's own ``Reload`` column intro).
``allowed_mcp`` is the OPPOSITE: a bare hand-edit of ``profile.yaml``
does NOTHING on its own — reapply only fires when something ELSE calls
:func:`~reyn.core.hot_reload.request_reload` (``/reload``, or an LLM
hooks-write op). Reusing ``hot`` would answer "does editing this file
alone make the change take effect?" with the WRONG polarity. Renamed to
``explicit-trigger`` — a new word for a genuinely different mechanism,
not the same one under a borrowed name.

## The 4 reload classes

Architect named 3: ``live`` / ``construction-once`` / ``restart``. This
slice's own measurement (each key traced to its real read call site, not
assumed) found a 4th shape that fits none of the 3: ``allowed_mcp`` is
neither read fresh on every access (``live``) nor frozen for a session's
lifetime (``construction-once``) nor does it need a full process restart
(``restart``) — see ``explicit-trigger``'s own entry below. Disclosed
here rather than silently forced into one of the 3 originally named
classes — architect's own explicit invitation on this issue: "1 件ずつ
確かめる前提" (measure one at a time; a 4th class turning out necessary
is an expected outcome of that process, not a deviation from it).

- ``LIVE`` — re-read on every access, no caching. A caller holding the
  value across an await must re-read it, never cache it (every LIVE key
  below carries this exact warning at its own read call site).
- ``CONSTRUCTION_ONCE`` — resolved exactly once per agent, at session
  construction. An already-running session does not notice a
  ``profile.yaml`` edit until its own NEXT construction (next
  ``--connect``/reattach) — neither ``LIVE`` nor a process restart.
- ``EXPLICIT_TRIGGER`` — a bare hand-edit of ``profile.yaml`` does
  NOTHING on its own; reapply only happens when something else calls
  :func:`~reyn.core.hot_reload.request_reload` (``/reload``, or an LLM
  hooks-write op), applied at the next turn boundary. Genuinely different
  from the project-layer ``restart / hot`` vocabulary's own ``hot`` —
  THAT class's defining property is the opposite polarity: the file write
  itself IS the trigger (``profile.yaml`` is not in ``_HOT_RELOAD_FILES``,
  confirmed against ``origin/main``).
- ``RESTART`` — takes effect only after a full process restart. No
  slice-1 key currently uses this value; kept in the vocabulary so a
  future key that genuinely needs it has somewhere to declare that,
  rather than being force-fit into one of the other 3.

Every value below was traced to its own real read call site during this
slice's own measurement pass — see each entry's own comment for the
file:line and the verbatim evidence quoted from that site's own
docstring, mirroring how the 3 pre-measured examples
(``_agent_profile_preferences`` / ``_read_base_dir_override`` /
``project_context_path``) were originally reported.
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields

LIVE = "live"
CONSTRUCTION_ONCE = "construction-once"
EXPLICIT_TRIGGER = "explicit-trigger"
RESTART = "restart"

#: :class:`~reyn.runtime.profile.AgentProfile` field names that are NOT an
#: independent "reload class" question — excluded from
#: :func:`declared_agent_profile_keys`'s derivation. ``name``/``created_at``
#: are identity/audit metadata (never re-read for behavior);
#: ``preferences``/``bounding`` are expanded into their own DOTTED keys
#: below (``preferences.<PREFERENCE_KEYS member>`` /
#: ``bounding.<BOUNDING_KEYS member>``) rather than named as top-level keys
#: in their own right — see :func:`declared_agent_profile_keys`.
_NON_RELOAD_CLASS_FIELDS: "frozenset[str]" = frozenset(
    {"name", "created_at", "preferences", "bounding"},
)

#: Every per-agent-profile key this slice measured, declared ONCE here —
#: the doc's own generated section (see
#: ``docs/reference/config/reyn-yaml.md``'s "Per-agent profile key reload
#: classes" section) and :func:`missing_reload_class_declarations`'s own
#: completeness gate both read this dict, never a second copy.
AGENT_PROFILE_RELOAD_CLASSES: "dict[str, str]" = {
    # AgentProfile's own top-level fields (name/created_at/preferences/
    # bounding excluded — see _NON_RELOAD_CLASS_FIELDS).
    "role": CONSTRUCTION_ONCE,  # agent.py: Agent is frozen, built once at construction
    "allowed_mcp": EXPLICIT_TRIGGER,  # session.py:_reapply_per_agent_capability, the per_agent_capability seam
    "base_dir": LIVE,  # session.py:_workspace_base_dir, "a live re-read on every access"
    "project_context_path": CONSTRUCTION_ONCE,  # registry_bootstrap.py:resolve_agent_project_context, owner ruling B/#3787
    # `preferences.*` — every PREFERENCE_KEYS entry, all resolved through
    # the SAME `_resolve_session_preference`/`warn_ratio_overrides` live
    # re-read (session.py, verbatim "Live re-read on every call (never
    # cached)").
    "preferences.output_language": LIVE,
    "preferences.chat.reasoning.display": LIVE,
    "preferences.cost.per_agent_tokens.warn_ratio": LIVE,
    "preferences.cost.per_agent_cost_usd.warn_ratio": LIVE,
    "preferences.cost.daily_tokens.warn_ratio": LIVE,
    "preferences.cost.daily_cost_usd.warn_ratio": LIVE,
    "preferences.cost.monthly_tokens.warn_ratio": LIVE,
    "preferences.cost.monthly_cost_usd.warn_ratio": LIVE,
    "preferences.cost.rate_limit_warn_ratio": LIVE,
    # `bounding.*` — the sole BOUNDING_KEYS entry, resolved through
    # `_agent_profile_bounding`/`compose_model_ceiling`, same live shape.
    "bounding.model": LIVE,
}


def declared_agent_profile_keys() -> "set[str]":
    """The real vocabulary :data:`AGENT_PROFILE_RELOAD_CLASSES` must cover
    — derived, never hand-duplicated (TESTS-READ A block ①, architect):
    :class:`~reyn.runtime.profile.AgentProfile`'s own fields, read via
    :func:`dataclasses.fields` (never a hand-typed literal list — a field
    added to that dataclass reaches this set for free, no edit here
    needed), minus :data:`_NON_RELOAD_CLASS_FIELDS`; plus every
    :data:`~reyn.runtime.preferences.PREFERENCE_KEYS` entry (prefixed
    ``preferences.``) and every :data:`~reyn.runtime.bounding.
    BOUNDING_KEYS` entry (prefixed ``bounding.``) — the SAME 2 registries
    :func:`~reyn.runtime.preferences.validate_preferences`/
    :func:`~reyn.runtime.bounding.validate_bounding` already gate
    ``profile.yaml`` writes against, so this vocabulary can never name a
    key those loaders would themselves reject."""
    from reyn.runtime.bounding import BOUNDING_KEYS
    from reyn.runtime.preferences import PREFERENCE_KEYS
    from reyn.runtime.profile import AgentProfile

    profile_keys = {
        f.name for f in dataclass_fields(AgentProfile)
    } - _NON_RELOAD_CLASS_FIELDS
    keys = set(profile_keys)
    keys |= {f"preferences.{k}" for k in PREFERENCE_KEYS}
    keys |= {f"bounding.{k}" for k in BOUNDING_KEYS}
    return keys


def missing_reload_class_declarations() -> "list[str]":
    """The real per-agent-profile keys with NO entry in
    :data:`AGENT_PROFILE_RELOAD_CLASSES` — sorted, empty when the
    declaration is complete. A key added to ``AgentProfile``/
    ``PREFERENCE_KEYS``/``BOUNDING_KEYS`` with no matching entry here
    shows up in this list — the completeness gate
    (``tests/runtime/test_4206_slice1_profile_reload_class.py``) fails
    the moment it is non-empty, the same "declare it or the gate reds"
    discipline #5190's own hook-kind registry uses."""
    return sorted(declared_agent_profile_keys() - set(AGENT_PROFILE_RELOAD_CLASSES))
