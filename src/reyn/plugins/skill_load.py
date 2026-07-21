"""Skill-load: invocation-time ``${REYN_*}``/``${CLAUDE_*}`` expansion for a
SKILL.md body (ADR 0064 §3.5, plugin-model P4, #3070).

**The seam.** Before this module, a skill body was read raw by the ordinary
``file`` read op (``reyn.core.op_runtime.file.handle``) — zero substitution,
the only capability with no invocation-time expansion pass (ADR §3.5). This
module supplies the missing pass; ``file.handle`` calls
:func:`load_skill_body` for exactly one file: the resolved read target whose
basename is the standard ``SKILL.md`` filename (:data:`SKILL_BODY_FILENAME`,
the agentskills.io convention ADR §3.6 honours as-is). Every other read
(a regular file, an ``L3`` bundled asset a skill's instructions reference)
is untouched — this is NOT a new execution surface (#2971's "no run_skill
verb" rationale still holds: reading is still the invocation, just no longer
byte-identical to what's on disk); it is the SAME read op, doing one more
thing to the content it already decoded.

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
body from the pre-plugin-model install path (``skill_management__install_*``,
no P2 bake at all) or for ``${REYN_SKILL_DIR}``/``${REYN_PROJECT_DIR}``
(never baked by either path), this is the ONLY place they resolve.
``${REYN_PROJECT_DIR}`` in particular MUST be resolved fresh on every
load — baking it anywhere would go stale the moment the operator points a
session at a different project, or a different project enables the same
globally-installed plugin.

**Plugin-root resolution.** A skill installed via
``skill_management__install_local``/``_install_source`` (the pre-plugin-model
skill install path, unrelated to a ``.reyn-plugin/plugin.json`` manifest) has
no separate plugin directory — its own directory IS the root for
``${REYN_PLUGIN_ROOT}`` purposes there, so :func:`resolve_plugin_root` falls
back to the skill's own directory when it finds no manifest walking upward.
A plugin-shipped skill (``<plugin_root>/skills/<name>/SKILL.md``) resolves
to the real, distinct plugin root — reusing P1's
``reyn.plugins.manifest.manifest_path_for`` to find the ``.reyn-plugin/``
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
from reyn.plugins.tokens import PluginTokenContext, expand_reyn_tokens

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

    #3196: this is NECESSARY but NOT SUFFICIENT for routing a read through
    skill-load expansion — filename alone used to be the whole gate (the
    vulnerability), letting an attacker-planted, unregistered ``SKILL.md``
    anywhere under the project root have its ``${env:VAR}`` tokens expanded
    to real secret values on an ORDINARY read. ``file.handle`` now ALSO
    requires the resolved path to fall into a registered provenance class
    (builtin / registered-plugin-body / config-registered skill entry —
    see ``file._skill_body_provenance``) before calling
    :func:`load_skill_body`. This predicate stays filename-only because it
    still answers its own narrow question correctly (is this file shaped
    like a skill body); it is simply no longer used alone as the trust
    gate.
    """
    return Path(path).name == SKILL_BODY_FILENAME


def resolve_plugin_root(skill_dir: Path) -> Path:
    """Find the plugin root a skill at *skill_dir* belongs to.

    Walks *skill_dir* and its parents looking for ``.reyn-plugin/plugin.json``
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
) -> "tuple[str, list[str], list[str]]":
    """Expand invocation-time ``${REYN_*}``/``${CLAUDE_*}``/``${env:...}``
    tokens in a decoded SKILL.md body (§3.5's "skill-load verb").

    ``content`` is the ALREADY-DECODED text of the file at *skill_path* (the
    caller — ``file.handle`` — has already run the decode ladder; this
    function does no I/O of its own and never re-reads the file).

    Returns ``(expanded_body, env_names_expanded, env_names_denied)`` — the
    caller returns *expanded_body* verbatim as the read op's `content`;
    the two name lists (#3198, superseding #3196's bare int count) are for
    the caller's audit-event ONLY (names + counts via ``len()``, NEVER the
    values) — never for display to the model.

    ``alias_claude`` should be ``True`` only when *skill_path* is known to be
    a Claude-authored SKILL.md (ADR §3.6's ingestion-boundary rule, mirroring
    ``expand_reyn_tokens``'s own parameter) — the caller decides that, this
    function just threads it through.

    ``permission_decl`` (#3198) gates ``${env:VAR}`` expansion specifically —
    ``None`` (the default) is treated as an EMPTY ``PermissionDecl``, i.e.
    NOTHING is allowlisted, so a caller that forgets to thread a real decl
    fails CLOSED (no env expansion at all), never open. Location tokens
    (``${REYN_*}``/``${CLAUDE_*}``, via ``expand_reyn_tokens`` above) are
    UNAFFECTED by this gate — they carry no credential, only positional
    metadata (ADR §3.4).
    """
    skill_dir = Path(skill_path).resolve().parent
    token_ctx = PluginTokenContext(
        plugin_root=resolve_plugin_root(skill_dir),
        project_dir=project_dir,
        skill_dir=skill_dir,
    )
    expanded = expand_reyn_tokens(content, token_ctx, alias_claude=alias_claude)
    return _expand_env_tokens(expanded, permission_decl)


__all__ = [
    "SKILL_BODY_FILENAME",
    "is_skill_body_path",
    "resolve_plugin_root",
    "load_skill_body",
]
