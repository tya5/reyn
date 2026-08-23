"""``${REYN_*}`` plugin/skill location-token expansion (ADR 0064 §3.4/§3.5/§3.6).

**A separate layer from ``expand_env`` (``security/secrets/interpolation.py``,
ADR-0030).** ``expand_env`` expands ``${VAR}`` from ``os.environ`` across an
MCP server's spawn config — a config-time env-injection concern. This module
expands a **different, fixed token vocabulary** (``REYN_PLUGIN_ROOT`` /
``REYN_SKILL_DIR`` / ``REYN_PROJECT_DIR``, plus the ``CLAUDE_*`` alias) whose
values come from an explicit :class:`PluginTokenContext`, never from
``os.environ``. The two layers are deliberately not merged (owner: "config
variable expansion is a separate thing") — a plugin author's ``${VAR}`` (an
env var they want at spawn time) and reyn's own ``${REYN_PLUGIN_ROOT}`` (a
location reyn resolves) must not be confusable with each other.

**Variable kind split (§3.4), uniform across mcp / pipeline / skill:**

- **stable location** (``REYN_PLUGIN_ROOT``, ``REYN_SKILL_DIR``) — fixed the
  instant a plugin is copied to its install dir; resolved ONCE at copy time
  and baked into the copied files (P2 concern). This module's
  :func:`expand_reyn_tokens` is the primitive that P2's copy step calls.
- **dynamic param** (``REYN_PROJECT_DIR``, plus ``${env:VAR}`` / per-run
  ``ctx`` params handled elsewhere) — only has a value at invocation
  (mcp spawn / pipeline run / skill-load, §3.5); expanded fresh each call,
  never baked into the copy.

Both kinds go through the SAME :func:`expand_reyn_tokens` call — the
distinction is in *when* the caller invokes it (copy time vs. invocation
time) and *which* :class:`PluginTokenContext` fields it supplies, not in the
expansion mechanism itself (no asymmetry between capability types, §3.4).

Any ``${...}`` token this module does not recognise (an env var for
``expand_env``, a pipeline ``ctx`` param, an unset field) is left untouched
— the two expansion layers compose by each ignoring what the other owns.

**#3629 — "stable location" only describes the copy-time bake, not
persistence.** ``REYN_SKILL_DIR``/``REYN_PLUGIN_ROOT`` being "resolved once
at copy time" says nothing about what happens to a VALUE this module
expands into a skill body at LOAD time (``skill_load.py``, invocation-time,
same as the dynamic params) once that expanded string is persisted to
``.reyn/agents/<id>/history.jsonl`` — history is immutable, so a rename or
move after that point leaves the OLD absolute path baked into an old
history entry forever, replayed to the model on every later turn with no
way to tell it apart from a live path. This module's mechanism (expand a
literal token into a value) is unaffected either way; the fix (#3629) lives
one layer up, in ``skill_load.py``/the history-persistence boundary: the
location tokens (``REYN_SKILL_DIR``/``REYN_PLUGIN_ROOT``, plus their
``CLAUDE_*`` aliases — see :data:`LOCATION_TOKEN_NAMES`) are kept literal
in what gets PERSISTED and re-expanded fresh, against the current
filesystem, every time a persisted turn is re-serialised for the wire
(``router_history_buffer.py``'s :func:`~reyn.plugins.skill_load.
refresh_location_tokens`) — the same "expanded fresh each call, never
baked into a durable copy" discipline this module already documents for
``REYN_PROJECT_DIR``, extended to the two tokens that were missing it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Matches ${VAR_NAME} — word chars only, mirrors interpolation.py's _ENV_VAR_RE
# shape so both layers share the same token *syntax*, not the same *vocabulary*.
_TOKEN_RE = re.compile(r"\$\{(\w+)\}")

# ``${CLAUDE_*}`` -> canonical ``${REYN_*}`` alias map (§3.6). Applied ONLY at
# ingestion of a Claude-authored plugin/skill — never unconditionally, so a
# reyn-native plugin never has to know the alias exists. Preserves the
# SKILL_DIR vs PLUGIN_ROOT distinction (both aliases resolve to their OWN
# reyn token, never collapsed to one root).
CLAUDE_ALIAS_MAP: dict[str, str] = {
    "CLAUDE_PLUGIN_ROOT": "REYN_PLUGIN_ROOT",
    "CLAUDE_SKILL_DIR": "REYN_SKILL_DIR",
    "CLAUDE_PROJECT_DIR": "REYN_PROJECT_DIR",
}

# #3629: the token NAMES (canonical + CLAUDE_* alias spellings) that must
# never be baked into what gets PERSISTED to history — see the module
# docstring's "stable location only describes the copy-time bake" note.
# ``REYN_PROJECT_DIR``/``CLAUDE_PROJECT_DIR`` are deliberately absent: the
# architect's #3629 measurement found PROJECT_DIR already safe (never
# baked into a durable copy, resolved fresh from the live workspace on
# every call) — only the two location tokens share SKILL_DIR's defect.
LOCATION_TOKEN_NAMES: frozenset[str] = frozenset({
    "REYN_SKILL_DIR", "REYN_PLUGIN_ROOT",
    "CLAUDE_SKILL_DIR", "CLAUDE_PLUGIN_ROOT",
})


@dataclass(frozen=True)
class PluginTokenContext:
    """The resolved values for one expansion pass.

    ``plugin_root``: the plugin's install directory (``~/.reyn/plugins/
    <name>/`` once P2 lands, or the local working-copy dir during the
    author/test loop, ADR §3.2). Stable location.

    ``project_dir``: the current project/workspace root — NOT reyn's own
    installed-package root (``reyn.runtime.reyn_repo.resolve_reyn_root``
    resolves reyn's own repo/wheel location, a different and unrelated
    concept from "the project the operator is working in"). Dynamic param:
    callers re-resolve this per invocation from the live session's
    workspace, they never bake it into a copied file.

    ``skill_dir``: a specific skill's directory within the plugin
    (``<plugin_root>/skills/<name>/``). ``None`` outside a skill-load
    context (§3.5) — expanding an MCP config or a pipeline never has a
    skill dir, so ``${REYN_SKILL_DIR}`` is correctly left unresolved there
    rather than silently defaulting to ``plugin_root`` (the SKILL_DIR vs
    PLUGIN_ROOT distinction §3.4/§3.6 calls out by name).
    """

    plugin_root: Path
    project_dir: Path
    skill_dir: Path | None = None

    def tokens(self) -> dict[str, str]:
        values = {
            "REYN_PLUGIN_ROOT": str(self.plugin_root),
            "REYN_PROJECT_DIR": str(self.project_dir),
        }
        if self.skill_dir is not None:
            values["REYN_SKILL_DIR"] = str(self.skill_dir)
        return values


def _resolve_token_map(ctx: PluginTokenContext, *, alias_claude: bool) -> dict[str, str]:
    values = ctx.tokens()
    if alias_claude:
        for claude_name, reyn_name in CLAUDE_ALIAS_MAP.items():
            if reyn_name in values:
                values[claude_name] = values[reyn_name]
    return values


def _expand_str(value: str, token_map: dict[str, str]) -> str:
    def _replace(m: re.Match) -> str:
        # group(1) is the (\w+) capture — always present on a match (the
        # pattern cannot match without it), so ``name`` is a concrete str.
        name: str = m.group(1)
        whole: str = m.group(0)
        # Unrecognised token (an expand_env var, a pipeline ctx param, an
        # unresolved dynamic param) is left as-is for a later expansion pass.
        return token_map.get(name, whole)

    return _TOKEN_RE.sub(_replace, value)


def expand_reyn_tokens(obj: Any, ctx: PluginTokenContext, *, alias_claude: bool = False) -> Any:
    """Recursively expand ``${REYN_*}`` (and, if ``alias_claude``, ``${CLAUDE_*}``)
    tokens in all string values of a dict / list / str tree.

    ``alias_claude`` must be ``True`` only in the code path ingesting a
    Claude-authored SKILL.md/plugin (§3.6) — never unconditionally, so a
    reyn-native plugin's own literal ``${CLAUDE_...}`` text (however
    unlikely) is never rewritten.

    Non-string scalars (int, bool, None, …) are returned unchanged, mirroring
    ``expand_env``'s shape.
    """
    token_map = _resolve_token_map(ctx, alias_claude=alias_claude)
    return _expand(obj, token_map)


def _expand(obj: Any, token_map: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return _expand_str(obj, token_map)
    if isinstance(obj, dict):
        return {k: _expand(v, token_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v, token_map) for v in obj]
    return obj


def resolve_token_map(ctx: PluginTokenContext, *, alias_claude: bool = False) -> dict[str, str]:
    """Public wrapper over the token-name → value map :func:`expand_reyn_tokens`
    substitutes from (#3629).

    Callers that need to expand a SUBSET of the recognised tokens (e.g.
    ``skill_load.py`` expanding ``REYN_PROJECT_DIR`` immediately while
    leaving the location tokens literal for persistence — see
    :data:`LOCATION_TOKEN_NAMES`) build their own partial map by filtering
    this one, then call :func:`expand_with_map` — never re-derive the
    value computation independently (``PluginTokenContext.tokens()`` +
    the ``CLAUDE_*`` alias rule stay the single source).
    """
    return _resolve_token_map(ctx, alias_claude=alias_claude)


def expand_with_map(obj: Any, token_map: dict[str, str]) -> Any:
    """Public wrapper over the same recursive substitution
    :func:`expand_reyn_tokens` uses internally, taking an already-resolved
    ``token_map`` directly (#3629) — for a caller that wants to expand only
    a SUBSET of the tokens :func:`resolve_token_map` would return (see that
    function's docstring). A token name absent from *token_map* is left
    untouched, exactly like an unrecognised token — the caller controls
    "subset" purely by which keys it omits, not by a second code path.
    """
    return _expand(obj, token_map)


# #5140: matches a REMAINING ``${REYN_*}``/``${CLAUDE_*}`` placeholder after
# an expansion pass — i.e. reyn's OWN token vocabulary, never an arbitrary
# caller-owned ``${VAR}`` (an env var meant for a spawned child process, a
# pipeline ``ctx`` param, …). Deliberately narrower than ``_TOKEN_RE``: a
# fail-close check built on the wrong regex here would refuse to load a
# hooks.yaml over a token it was never reyn's job to resolve in the first
# place (acceptance ③ — a non-reyn ``${FOO}`` must load exactly as before).
_UNRESOLVED_REYN_TOKEN_RE = re.compile(r"\$\{((?:REYN|CLAUDE)_\w+)\}")


def find_unresolved_reyn_tokens(obj: Any) -> list[str]:
    """Recursively collect every REMAINING ``${REYN_*}``/``${CLAUDE_*}``
    placeholder in *obj* (post-:func:`expand_with_map`, typically).

    #5140: the fail-close half of that issue's ruling — reyn's own token
    vocabulary left unresolved after reyn's own expansion pass means reyn
    could not supply a value it owns, which is reyn's bug, not an
    operator's config choice (contrast with an arbitrary ``${VAR}``, which
    a downstream consumer — e.g. a spawned child process inheriting the
    env — may legitimately resolve later; this function does not flag
    those at all). A caller finding this list non-empty should refuse to
    use the expanded structure rather than silently proceed with an
    empty/wrong value standing in for an unresolved token — see
    ``config/loader.py``'s ``load_per_agent_hooks`` for the reference
    caller."""
    if isinstance(obj, str):
        return [m.group(0) for m in _UNRESOLVED_REYN_TOKEN_RE.finditer(obj)]
    if isinstance(obj, dict):
        found: list[str] = []
        for v in obj.values():
            found.extend(find_unresolved_reyn_tokens(v))
        return found
    if isinstance(obj, list):
        found = []
        for v in obj:
            found.extend(find_unresolved_reyn_tokens(v))
        return found
    return []
