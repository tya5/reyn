"""Skill-load: invocation-time ``${REYN_*}``/``${CLAUDE_*}`` expansion for a
SKILL.md body (ADR 0064 §3.5, plugin-model P4, #3070).

**The seam.** Originally (#2971) a skill body was read raw by the ordinary
``file`` read op, special-cased inline (``is_skill_body_path`` routed
exactly the ``SKILL.md`` filename through this module's
:func:`load_skill_body`) — the only capability with no invocation-time
expansion pass of its own (ADR §3.5's original point). **FP-0066 P0
(#3247)** extracted that responsibility OUT of the general-purpose file
handler into the dedicated ``load_skill`` op
(``reyn.core.op_runtime.load_skill``), which now owns the WHOLE call —
provenance classification, permission gate, the #3196 resolve-once, and
this module's expansion primitives. ``file.read`` is a plain read again for
every path, including a ``SKILL.md``-named one. The operator-explicit
``:name`` invocation path (``reyn.interfaces.skill_invoke.
resolve_skill_body``) has its own separate, lightweight call site over
these SAME primitives — it never went through ``file.read`` either, before
or after this extraction. Every other read (a regular file, an ``L3``
bundled asset a skill's instructions reference) is untouched by any of
this — this is still NOT a new execution surface (#2971's "no run_skill
verb" rationale still holds: loading is still the invocation, just via its
own dedicated op now instead of piggybacking on ``file.read``).

**Reuses P1's token layer verbatim** (``reyn.plugins.tokens`` —
:func:`~reyn.plugins.tokens.expand_reyn_tokens` /
:class:`~reyn.plugins.tokens.PluginTokenContext`), per the standing
"no reinventing existing functionality" rule. This module supplies only what
P1 could not: WHERE a given skill's location-token values come from at
invocation time.

**Location vars are resolved here too, redundantly with P2's copy-time
bake — deliberately.** ADR §3.4 designates ``${REYN_PLUGIN_ROOT}``/
``${REYN_SKILL_DIR}`` "stable location, baked at copy time". P2
(``plugin_install``'s ``_expand_plugin_files``, already merged) bakes
``${REYN_PLUGIN_ROOT}`` into every ``skills/*/SKILL.md`` at copy time —
but deliberately does NOT bake ``${REYN_SKILL_DIR}`` there (no per-skill
``skill_dir`` on the whole-plugin bake pass) and, since #3070, does NOT bake
``${REYN_PROJECT_DIR}`` into a skill body either (a global
``~/.reyn/plugins/<name>/`` copy can be ENABLED into many different
projects, §3.3 — baking one install call's project into the shared copy
would freeze every future enabling project to whichever one installed it
first). This module resolves ALL THREE tokens on every load regardless:
for a body P2 already baked ``${REYN_PLUGIN_ROOT}`` into, a second pass
through the same expander is a no-op (no ``${...}`` left to match); for a
body from the pre-plugin-model install path (``skill_install_*``,
no P2 bake at all) or for ``${REYN_SKILL_DIR}``/``${REYN_PROJECT_DIR}``
(never baked by either path), this is the ONLY place they resolve.
``${REYN_PROJECT_DIR}`` in particular MUST be resolved fresh on every
load — baking it anywhere would go stale the moment the operator points a
session at a different project, or a different project enables the same
globally-installed plugin.

**Plugin-root resolution.** A skill installed via
``skill_install_local``/``_install_source`` (the pre-plugin-model
skill install path, unrelated to a plugin ``plugin.json`` manifest) has
no separate plugin directory — its own directory IS the root for
``${REYN_PLUGIN_ROOT}`` purposes there, so :func:`resolve_plugin_root` falls
back to the skill's own directory when it finds no manifest walking upward.
A plugin-shipped skill (``<plugin_root>/skills/<name>/SKILL.md``) resolves
to the real, distinct plugin root — reusing P1's
``reyn.plugins.manifest.manifest_path_for`` to find the ``plugin.json``
marker rather than re-deriving the plugin layout convention independently.

**``${env:VAR}`` — a NAMESPACED env-var token, deliberately NOT
``expand_env``'s bare ``${VAR}`` syntax.** ADR §3.4's table lists
``${env:VAR}`` (with the ``env:`` prefix, literally) as skill-load's dynamic
os.environ bucket. A skill body is free-form Markdown prose an author writes
— unlike an mcp spawn config or a pipeline yaml (structured values authors
already expect to template), a skill body routinely contains literal
``${VAR}``-shaped text in code-block examples (shell snippets, other tools'
config samples). Reusing ``expand_env``'s bare-``${VAR}`` syntax here would
silently mangle that prose (blank out an unset "variable" that was never
meant to be one) and emit spurious ``UserWarning``s for every such example.
The ``env:`` prefix is the disambiguator — this module owns exactly
``${env:VAR_NAME}`` and reads directly from ``os.environ`` (unset → left
untouched, NOT blanked, so an author's stray ``${env:...}``-shaped prose
degrades to "unexpanded token" rather than "silently deleted text"); it does
NOT call ``reyn.security.secrets.interpolation.expand_env`` (that remains
scoped to mcp spawn config, its own established call site, ADR-0030).

**#3198: ``${env:VAR}`` expansion is gated by a deny-by-default allowlist —
NOT a bare filename-triggered ``os.environ`` read.** #3196/#3199 gated WHICH
SKILL.md bodies get expanded at all (provenance: builtin / registered-plugin /
config-registered entry). This closes the ORTHOGONAL question of WHAT a
body that clears that gate may read: without this, a REGISTERED skill could
still write ``${env:GITHUB_TOKEN}`` in its own prose and have it expanded
into the LLM's context on an ordinary read — installing a plugin would be
equivalent to handing it every credential in the process environment.
``load_skill_body``/``_expand_env_tokens`` now take a ``permission_decl``
(``reyn.security.permissions.permissions.PermissionDecl``); a name is
substituted only when ``PermissionResolver.is_env_expand_allowed`` (or the
equivalent direct ``EffectivePermission`` check) says so — reusing the SAME
axis-of-permission model ``secret_write`` already established for the
write-side, per the "no reinventing existing functionality" rule, rather
than a bespoke allowlist config surface. A denied token is left in the
output UNEXPANDED (never blanked, mirroring the existing unset-var
behavior) — "not on the allowlist" and "not set in the environment" both
degrade to the same harmless "stray unexpanded token" shape, never a hard
read failure. ``permission_decl`` defaults to ``None`` (treated as an EMPTY
decl, i.e. nothing declared) so any caller that forgets to thread a real
decl fails CLOSED, not open.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.plugins.manifest import manifest_path_for
from reyn.plugins.tokens import (
    LOCATION_TOKEN_NAMES,
    PluginTokenContext,
    expand_with_map,
    resolve_token_map,
)

if TYPE_CHECKING:
    from reyn.security.permissions.permissions import PermissionDecl

# The one filename this module routes through skill-load expansion instead
# of a byte-identical read (agentskills.io convention, ADR §3.6). Matched on
# basename only — a skill's containing directory name is never dictated by
# the standard, only the body filename inside it is.
SKILL_BODY_FILENAME = "SKILL.md"

# ``${env:VAR_NAME}`` — namespaced so it cannot collide with a plain
# ``${VAR}`` example a skill author writes in prose (see module docstring).
_ENV_TOKEN_RE = re.compile(r"\$\{env:(\w+)\}")


def _expand_env_tokens(
    text: str, permission_decl: "PermissionDecl | None",
) -> "tuple[str, list[str], list[str]]":
    """Expand ``${env:VAR}`` from ``os.environ`` — gated by *permission_decl*'s
    ``env_expand`` allowlist (#3198, deny-by-default), unset OR undeclared
    both leave the token untouched (never blanks skill-body prose; see
    module docstring).

    Returns ``(expanded_text, expanded_names, denied_names)``:

    - *expanded_names* — the ``VAR`` name of every token ACTUALLY
      substituted (env var was set AND allowlisted). May contain
      duplicates if the same name is referenced more than once — the count
      the caller's audit-event wants is "how many substitutions happened",
      not "how many distinct names".
    - *denied_names* — the ``VAR`` name of every ``${env:VAR}``-shaped token
      that was REJECTED by the allowlist (left unexpanded). The allowlist
      check runs BEFORE the ``os.environ`` lookup, so a name that is both
      UNSET *and* not allowlisted still counts as denied here (an empty
      allowlist denies every name regardless of whether it happens to be
      set — that is the deny-by-default property #3198 exists to
      guarantee). Only a name that IS allowlisted but simply unset is
      excluded from *denied_names* (nothing was refused; there was nothing
      to grant or refuse — see the unset-but-allowlisted test).

    NEVER returns a value — only names, per #3198's audit-event mandate (a
    denial log or an expansion count must not become a second place a
    secret's value could leak).
    """
    from reyn.security.permissions.permissions import PermissionDecl, env_expand_allowed

    decl = permission_decl if permission_decl is not None else PermissionDecl()
    expanded_names: list[str] = []
    denied_names: list[str] = []

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        if not env_expand_allowed(decl, name):
            denied_names.append(name)
            return m.group(0)
        value = os.environ.get(name)
        if value is None:
            return m.group(0)
        expanded_names.append(name)
        return value

    expanded = _ENV_TOKEN_RE.sub(_replace, text)
    return expanded, expanded_names, denied_names


def is_skill_body_path(path: "str | Path") -> bool:
    """True when *path*'s filename is the standard SKILL.md body filename.

    #3196: this is NECESSARY but NOT SUFFICIENT for routing a load through
    skill-load expansion — filename alone used to be the whole gate (the
    vulnerability), letting an attacker-planted, unregistered ``SKILL.md``
    anywhere under the project root have its ``${env:VAR}`` tokens expanded
    to real secret values on an ORDINARY read. The ``load_skill`` op
    (``reyn.core.op_runtime.load_skill``, FP-0066 P0/#3247) ALSO requires
    the resolved path to fall into a registered provenance class (builtin /
    registered-plugin-body / config-registered skill entry) before calling
    :func:`load_skill_body`. This predicate stays filename-only because it
    still answers its own narrow question correctly (is this file shaped
    like a skill body); it is simply no longer used alone as the trust
    gate. (Prior to #3247 this same check + gate lived inline in
    ``reyn.core.op_runtime.file``; it is unchanged in shape, just relocated
    to its own dedicated op.)
    """
    return Path(path).name == SKILL_BODY_FILENAME


def resolve_plugin_root(skill_dir: Path) -> Path:
    """Find the plugin root a skill at *skill_dir* belongs to.

    Walks *skill_dir* and its parents looking for ``plugin.json``
    (P1's :func:`~reyn.plugins.manifest.manifest_path_for` — the SAME marker
    P2's install step will write, not a re-derived convention). Returns the
    first directory found; falls back to *skill_dir* itself (already
    ``.resolve()``d) when no manifest is found anywhere above it — a
    standalone (non-plugin) skill's own directory is its own root.
    """
    current = skill_dir.resolve()
    for candidate in (current, *current.parents):
        if manifest_path_for(candidate).is_file():
            return candidate
    return current


def load_skill_body(
    content: str,
    *,
    skill_path: "str | Path",
    project_dir: Path,
    alias_claude: bool = False,
    permission_decl: "PermissionDecl | None" = None,
) -> "tuple[str, str, dict[str, str], list[str], list[str]]":
    """Expand invocation-time ``${REYN_*}``/``${CLAUDE_*}``/``${env:...}``
    tokens in a decoded SKILL.md body (§3.5's "skill-load verb").

    ``content`` is the ALREADY-DECODED text of the file at *skill_path* (the
    caller — ``file.handle`` — has already run the decode ladder; this
    function does no I/O of its own and never re-reads the file).

    Returns ``(expanded_body, persisted_body, location_token_map,
    env_names_expanded, env_names_denied)``:

    - *expanded_body* — EVERY recognised token substituted (unchanged shape
      from before #3629). The caller returns this verbatim as the read op's
      current-turn `content` — what the model reads THIS turn is exactly as
      correct as it always was; nothing about immediate usability changes.
    - *persisted_body* — ``REYN_PROJECT_DIR``/``CLAUDE_PROJECT_DIR``
      substituted (measured safe: never baked into a durable copy, #3629
      architect ruling), but ``REYN_SKILL_DIR``/``REYN_PLUGIN_ROOT`` (+
      their ``CLAUDE_*`` aliases, :data:`~reyn.plugins.tokens.
      LOCATION_TOKEN_NAMES`) left LITERAL. This is what the caller
      persists to ``history.jsonl`` instead of *expanded_body* — a rename
      or move after this turn can never freeze a now-dead absolute path
      into the durable record, because the record never held one.
    - *location_token_map* — the SAME token → value mapping that would have
      baked the location tokens into *expanded_body*, for the caller to
      stash on the persisted entry's ``meta`` (audit-completeness: since
      LLM-payload trace dumping is opt-in (``REYN_LLM_TRACE_DUMP``), history
      is the only ALWAYS-ON record of what a given turn actually resolved
      to). This map is NOT a re-expansion source on replay —
      :func:`refresh_location_tokens` re-derives fresh values from the
      CURRENT filesystem every time, never from this frozen snapshot; see
      that function's docstring.
    - *env_names_expanded* / *env_names_denied* — as before (#3198,
      superseding #3196's bare int count): names + counts via ``len()``
      ONLY, for the caller's audit-event, never the values, never for
      display to the model. Reported against *expanded_body* — the
      current-turn text actually shown to the model — since that is the
      one substitution round whose count matters for the audit event.

    ``alias_claude`` should be ``True`` only when *skill_path* is known to be
    a Claude-authored SKILL.md (ADR §3.6's ingestion-boundary rule, mirroring
    ``expand_reyn_tokens``'s own parameter) — the caller decides that, this
    function just threads it through.

    ``permission_decl`` (#3198) gates ``${env:VAR}`` expansion specifically —
    ``None`` (the default) is treated as an EMPTY ``PermissionDecl``, i.e.
    NOTHING is allowlisted, so a caller that forgets to thread a real decl
    fails CLOSED (no env expansion at all), never open. Location tokens
    (``${REYN_*}``/``${CLAUDE_*}``) are UNAFFECTED by this gate — they carry
    no credential, only positional metadata (ADR §3.4).
    """
    skill_dir = Path(skill_path).resolve().parent
    token_ctx = PluginTokenContext(
        plugin_root=resolve_plugin_root(skill_dir),
        project_dir=project_dir,
        skill_dir=skill_dir,
    )
    full_map = resolve_token_map(token_ctx, alias_claude=alias_claude)
    location_map = {k: v for k, v in full_map.items() if k in LOCATION_TOKEN_NAMES}
    non_location_map = {k: v for k, v in full_map.items() if k not in LOCATION_TOKEN_NAMES}

    persisted = expand_with_map(content, non_location_map)
    expanded = expand_with_map(persisted, location_map)

    expanded, env_expanded, env_denied = _expand_env_tokens(expanded, permission_decl)
    # Same substitution, applied to the persist-safe variant too — ``${env:...}``
    # expansion is orthogonal to the location-token split (a different regex,
    # #3198's own security gate, no interaction either way) and must not
    # differ between what the model reads this turn and what gets persisted.
    persisted, _persisted_env_expanded, _persisted_env_denied = _expand_env_tokens(
        persisted, permission_decl,
    )
    return expanded, persisted, location_map, env_expanded, env_denied


def refresh_location_tokens(
    content: str, *, skill_source_path: str, project_dir: Path, alias_claude: bool = False,
) -> str:
    """Re-expand ``${REYN_SKILL_DIR}``/``${REYN_PLUGIN_ROOT}`` (+ ``CLAUDE_*``
    aliases) in a PERSISTED skill-body history entry, against the CURRENT
    filesystem, at wire-serialise time (#3629).

    This is the "dynamic param" half of the location-token fix — the
    ``REYN_PROJECT_DIR`` discipline (ADR §3.4: "only has a value at
    invocation... expanded fresh each call, never baked into a durable
    copy") extended to the two tokens ``load_skill_body`` now leaves literal
    in what it persists. *skill_source_path* is the IDENTITY the original
    ``load_skill`` call resolved against (``op.path`` — typically
    project-relative, exactly as the model gave it), never the frozen
    absolute VALUE from that turn's ``location_token_map`` — an identity can
    be re-resolved fresh; a frozen value can only be repeated.

    Fresh resolution runs the EXACT SAME two calls the original load did
    (:func:`resolve_plugin_root` off ``Path(skill_source_path).parent``) —
    no separate mechanism, no drift risk between write-time and replay-time
    resolution.

    Three outcomes, by design, no other:

    - *skill_source_path* still resolves to a real file (nothing moved, OR
      the same relative structure exists in THIS run's checkout even
      though a PRIOR run's checkout was different — the "two working
      copies" / "different machine" case #3629 names explicitly) →
      substituted with the CURRENT value. Self-heals completely.
    - *skill_source_path* no longer resolves to anything (the file itself
      was renamed/deleted — #3629's actual reported case, #3588's skill
      rename) → the token is left LITERAL, unexpanded. This is NOT a
      partial fix masquerading as success: an unexpanded ``${REYN_SKILL_DIR}``
      is unambiguously a placeholder to the model, never mistaken for a
      live path the way a frozen absolute string was (the exact defect
      #3629 reports) — the class of failure changes from "silently wrong"
      to "visibly unresolved", which is the improvement this function
      claims, no more.
    - *content* has no location token left to match (a non-skill turn, or
      one that never had one) → returned unchanged, cheap no-op.

    Never touches already-poisoned pre-#3629 history — those entries were
    persisted with the token ALREADY baked into ``content`` as a plain
    string; there is no token left for this function to find, by
    construction (#3629 architect ruling: existing poisoned rows are
    intentionally never rewritten or annotated).
    """
    candidate = Path(skill_source_path)
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    try:
        exists = candidate.is_file()
    except OSError:
        exists = False
    if not exists:
        return content
    skill_dir = candidate.resolve().parent
    token_ctx = PluginTokenContext(
        plugin_root=resolve_plugin_root(skill_dir),
        project_dir=project_dir,
        skill_dir=skill_dir,
    )
    full_map = resolve_token_map(token_ctx, alias_claude=alias_claude)
    location_map = {k: v for k, v in full_map.items() if k in LOCATION_TOKEN_NAMES}
    return expand_with_map(content, location_map)


__all__ = [
    "SKILL_BODY_FILENAME",
    "is_skill_body_path",
    "resolve_plugin_root",
    "load_skill_body",
    "refresh_location_tokens",
]
