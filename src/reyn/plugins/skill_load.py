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
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from reyn.plugins.manifest import manifest_path_for
from reyn.plugins.tokens import PluginTokenContext, expand_reyn_tokens

# The one filename this module routes through skill-load expansion instead
# of a byte-identical read (agentskills.io convention, ADR §3.6). Matched on
# basename only — a skill's containing directory name is never dictated by
# the standard, only the body filename inside it is.
SKILL_BODY_FILENAME = "SKILL.md"

# ``${env:VAR_NAME}`` — namespaced so it cannot collide with a plain
# ``${VAR}`` example a skill author writes in prose (see module docstring).
_ENV_TOKEN_RE = re.compile(r"\$\{env:(\w+)\}")


def _expand_env_tokens(text: str) -> str:
    """Expand ``${env:VAR}`` from ``os.environ`` — unset leaves the token
    untouched (never blanks skill-body prose; see module docstring)."""

    def _replace(m: re.Match) -> str:
        value = os.environ.get(m.group(1))
        return value if value is not None else m.group(0)

    return _ENV_TOKEN_RE.sub(_replace, text)


def is_skill_body_path(path: "str | Path") -> bool:
    """True when *path*'s filename is the standard SKILL.md body filename —
    the one signal ``file.handle`` uses to route a read through skill-load
    expansion rather than a raw pass-through."""
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
) -> str:
    """Expand invocation-time ``${REYN_*}``/``${CLAUDE_*}``/``${env:...}``
    tokens in a decoded SKILL.md body (§3.5's "skill-load verb").

    ``content`` is the ALREADY-DECODED text of the file at *skill_path* (the
    caller — ``file.handle`` — has already run the decode ladder; this
    function does no I/O of its own and never re-reads the file). Returns the
    expanded body; the caller returns it verbatim as the read op's `content`.

    ``alias_claude`` should be ``True`` only when *skill_path* is known to be
    a Claude-authored SKILL.md (ADR §3.6's ingestion-boundary rule, mirroring
    ``expand_reyn_tokens``'s own parameter) — the caller decides that, this
    function just threads it through.
    """
    skill_dir = Path(skill_path).resolve().parent
    token_ctx = PluginTokenContext(
        plugin_root=resolve_plugin_root(skill_dir),
        project_dir=project_dir,
        skill_dir=skill_dir,
    )
    expanded = expand_reyn_tokens(content, token_ctx, alias_claude=alias_claude)
    return _expand_env_tokens(expanded)


__all__ = [
    "SKILL_BODY_FILENAME",
    "is_skill_body_path",
    "resolve_plugin_root",
    "load_skill_body",
]
