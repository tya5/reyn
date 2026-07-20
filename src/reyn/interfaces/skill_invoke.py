"""``:skill`` — operator-explicit skill invocation, a namespace separate from
``/`` slash commands (#3100).

**Why a separate namespace (Axis 4, architect-firm design).** Claude Code's
`/skill-name` overloads the SAME `/` prefix as its built-in commands, so a
skill can silently shadow a built-in (or vice versa) via undocumented
scope precedence — the root cause of Claude Code #13586. Reyn splits skills
onto their own `:` prefix (``reyn.interfaces.slash`` owns `/`; this module
owns `:`) so "is this a skill or a built-in?" is a **closed-type syntactic
distinction**, not a runtime precedence lookup. Collisions WITHIN the `:`
namespace (same skill name declared in two config tiers) still exist —
those are handled LOUDLY (see :data:`SKILL_STACK_MAX` callers /
``Session._skill_collisions``), never silently.

**Reuses the existing file-read-skill mechanism — no new execution surface.**
#2971 established that "invoking" a skill is reading its ``SKILL.md`` with
the ordinary file-read op and letting the model follow the body; there is
NO ``skill__<name>`` dispatch and this module does not add one.
:func:`resolve_skill_body` reads the SAME way the ordinary ``file`` read op's
skill-load pass does (``reyn.plugins.skill_load.load_skill_body``,
``reyn.builtin.docs.read_builtin_body_bytes`` for builtin-provenance paths) —
this module only supplies the trailing-args substitution the ordinary read
op has no reason to know about.

**No new permission gate.** `:skillname` names come from the operator's OWN
registered ``skills.entries`` — a set the operator already declared in
config or installed through a permission-gated ``skill_management__install_*``
call. Resolving `:name` to that entry's path and reading it grants no
capability beyond what the operator already put there themselves; there is
no LLM in the loop deciding which file to read. This mirrors the "operator
typing a path in their own terminal needs no gate" reasoning the file-read
op's ``_in_default_read_zone`` carve-out already encodes for project-local
reads.

**Axis 1 — parameter passing (Claude-Code-standard, owner-ratified).**
``$ARGUMENTS`` (the whole trailing raw text) / ``$0``/``$1``/... (shell-style
positional split of the trailing text) / ``$name`` (frontmatter
``arguments:`` named positions) / ``\\$`` escapes a literal ``$``. See
:func:`substitute_arguments` for the injection-safety invariant.

**Axis 3 — stacking.** ``:a :b <trailing>`` invokes both skills in ONE turn
(one LLM wake, #3100 Axis 2), capped at :data:`SKILL_STACK_MAX` (=6,
Claude Code's own limit). Expansion stops at the first token that isn't a
``:name``-shaped token; everything from there on (including a *further*
``:something`` if the cap was already hit) is trailing raw text, not a
7th stacked skill.

**Axis 5 — unknown name.** :func:`suggest_unknown_skill` never resolves to a
silent no-op; the caller (``Session._maybe_handle_skill_invoke``) always
renders an explicit, actionable error.

**Deferred (owner's own "final confirmation" list in #3100, not yet
settled):** the ``disable-model-invocation`` frontmatter flag is
intentionally NOT read by this module. Enforcing it (excluding a skill from
the L1 menu / ``skill_list`` while keeping it `:`-reachable) requires reading
every registered skill's frontmatter at prompt-build time, which conflicts
with the "on_demand costs nothing until read" design invariant
(``docs/concepts/tools-integrations/skills.md``) — it needs its own caching
design, not a bolt-on here. ``arguments`` is fully wired (it drives the
``$name`` substitution in :func:`substitute_arguments`). ``argument-hint``
is **parsed only** (:func:`read_skill_frontmatter_meta` reads it into
:class:`SkillFrontmatterMeta`) but **not yet consumer-wired** — the TUI `:`
completion (:func:`skill_invoke_completions`) surfaces each candidate's
``description``, NOT its ``argument_hint``, and no other surface reads it.
Displaying ``argument_hint`` as a TUI hint (the way the `/`-command picker
shows a ``usage`` line) is an **open gap**, tracked the same way as
``disable-model-invocation`` above — parsed so a SKILL.md author can already
declare it, wired to a consumer in a follow-up.
"""
from __future__ import annotations

import difflib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from reyn.core.frontmatter import split_frontmatter

if TYPE_CHECKING:
    from reyn.data.skills.registry import SkillEntry

# Claude Code's own stacking cap (architect-firm design, "standard v2.1.199+
# compliant"). Not operator-configurable — a fixed ceiling keeps `:a :b :c
# :d :e :f :g` from silently degrading into "the 7th+ name is now trailing
# text" without an obvious reason; six is the point Claude Code itself
# already draws the line.
SKILL_STACK_MAX = 6

_NAME_TOKEN_RE = re.compile(r"^:([A-Za-z0-9_-]+)(.*)$", re.DOTALL)


@dataclass(frozen=True)
class ParsedSkillInvocation:
    """A parsed `:a :b <trailing>` stack. ``names`` preserves invocation order
    (duplicates allowed — resolution/collision handling happens downstream)."""

    names: "tuple[str, ...]"
    trailing: str


def parse_skill_invocation(text: str) -> "ParsedSkillInvocation | None":
    """Parse a leading ``:name [:name2 ...] [trailing text]`` stack.

    Returns ``None`` when *text* does not start with a ``:name``-shaped
    token at all — the caller (``Session._handle_user_message``) then falls
    through to ordinary message handling untouched, so a message that merely
    starts with a literal colon in prose (rare, but not impossible) is never
    misrouted into an "unknown skill" error. Once at least one valid
    ``:name`` token IS recognized, this always returns a
    :class:`ParsedSkillInvocation` — a subsequent unresolvable NAME is an
    explicit-error case for the caller (Axis 5), not a re-parse failure.
    """
    remaining = text
    names: list[str] = []
    while len(names) < SKILL_STACK_MAX:
        stripped = remaining.lstrip(" \t")
        if not stripped.startswith(":"):
            break
        m = _NAME_TOKEN_RE.match(stripped)
        if not m:
            break
        names.append(m.group(1))
        remaining = m.group(2)
    if not names:
        return None
    return ParsedSkillInvocation(names=tuple(names), trailing=remaining.strip())


# ── frontmatter extensions (Axis 1 / Axis 6) ────────────────────────────────


@dataclass(frozen=True)
class SkillArgSpec:
    name: str
    description: str = ""


@dataclass(frozen=True)
class SkillFrontmatterMeta:
    """The `:`-invocation-relevant subset of a SKILL.md's frontmatter —
    Claude-Code-standard extensions on top of the ``name``/``description``
    pair ``reyn.core.op_runtime.skill_install`` already reads at install
    time. Parsed FRESH at invocation (not cached on the config-declared
    ``SkillEntry``), since ``arguments``/``argument-hint`` only matter once
    the operator is actually invoking, not at registration."""

    arguments: "tuple[SkillArgSpec, ...]" = ()
    argument_hint: str = ""


def read_skill_frontmatter_meta(raw_content: str) -> SkillFrontmatterMeta:
    """Extract ``arguments:``/``argument-hint:`` from a decoded SKILL.md's
    frontmatter. Lenient: a missing/malformed block yields the all-empty
    default rather than raising — a skill author's `:`-unaware SKILL.md
    (no ``arguments:`` at all) is the common case, not an error."""
    fm, _body = split_frontmatter(raw_content)
    if not isinstance(fm, dict):
        return SkillFrontmatterMeta()

    raw_args = fm.get("arguments")
    args: list[SkillArgSpec] = []
    if isinstance(raw_args, list):
        for item in raw_args:
            if isinstance(item, dict) and item.get("name"):
                args.append(SkillArgSpec(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                ))
            elif isinstance(item, str) and item.strip():
                args.append(SkillArgSpec(name=item.strip()))

    hint_raw = fm.get("argument-hint", fm.get("argument_hint", ""))
    hint = str(hint_raw or "")

    return SkillFrontmatterMeta(arguments=tuple(args), argument_hint=hint)


# ── $ARGUMENTS / $N / $name substitution (Axis 1) ───────────────────────────

# A sentinel astronomically unlikely to collide with real skill-body content
# — used only to protect a ``\$`` escape across the single substitution pass.
_ESCAPED_DOLLAR_SENTINEL = "\x00REYN_SKILL_INVOKE_ESCAPED_DOLLAR\x00"

_PLACEHOLDER_RE = re.compile(r"\$(ARGUMENTS|[0-9]+|[A-Za-z_][A-Za-z0-9_]*)")


def substitute_arguments(
    body: str, *, trailing: str, arg_spec: "Sequence[SkillArgSpec]" = (),
) -> str:
    """Literal, scoped ``$ARGUMENTS``/``$0``/``$1``/``$name`` substitution.

    **Injection-safety invariant (co-vet witness surface).** This function
    performs exactly ONE textual splice pass over *body* and never re-scans
    its OWN output for further ``$``/``${...}`` tokens — Python's
    ``re.sub`` does not recursively re-apply a pattern to a replacement
    string, so a value drawn from *trailing* that happens to itself look
    like ``$ARGUMENTS`` or ``${REYN_PROJECT_DIR}`` is inserted **verbatim,
    inert** — it is never re-interpreted as a further placeholder or a
    ``${REYN_*}`` skill-load token. Callers MUST run skill-load token
    expansion (``reyn.plugins.skill_load.load_skill_body``) on *body*
    BEFORE calling this, and MUST NOT run it again afterward — operator-
    controlled *trailing* text must never pass through a second token-
    expansion pass, which is the only way it could smuggle a
    ``${REYN_PROJECT_DIR}``-shaped string into an actual expansion.

    An unmatched placeholder (e.g. ``$2`` when only one positional arg was
    given, or ``$typo`` with no matching ``arguments:`` entry) is left
    untouched in the output — never blanked (mirrors skill-load's own
    ``${env:VAR}`` unset-token convention).
    """
    protected = body.replace("\\$", _ESCAPED_DOLLAR_SENTINEL)

    try:
        positional = shlex.split(trailing) if trailing.strip() else []
    except ValueError:
        # Unbalanced quotes in operator input — degrade to whitespace split
        # rather than raising mid-turn; a malformed quote is a UX papercut,
        # not a reason to abort the invocation.
        positional = trailing.split()

    position_by_name = {spec.name: i for i, spec in enumerate(arg_spec)}

    def _replace(m: "re.Match[str]") -> str:
        token = m.group(1)
        if token == "ARGUMENTS":
            return trailing
        if token.isdigit():
            idx = int(token)
            return positional[idx] if idx < len(positional) else m.group(0)
        idx = position_by_name.get(token)
        if idx is not None and idx < len(positional):
            return positional[idx]
        return m.group(0)

    expanded = _PLACEHOLDER_RE.sub(_replace, protected)
    return expanded.replace(_ESCAPED_DOLLAR_SENTINEL, "$")


# ── body resolution (reuses the existing file-read-skill mechanism) ────────


def resolve_skill_body(path: str, *, project_dir: Path) -> str:
    """Read + skill-load-expand a SKILL.md body.

    Reuses the SAME primitives the ordinary ``file`` read op's skill-load
    pass uses (``reyn.core.op_runtime.file.handle``, #3070/ADR-0064 §3.5) —
    ``read_builtin_body_bytes`` for a builtin-provenance path,
    ``read_plugin_body_bytes`` for a registered-plugin-provenance path, a
    plain filesystem read otherwise, then ``load_skill_body`` for
    ``${REYN_*}``/``${CLAUDE_*}``/``${env:...}`` token expansion. This is
    deliberately NOT a second read mechanism — #2971's "reading is the
    invocation" holds for `:` too.
    """
    from reyn.builtin.docs import read_builtin_body_bytes
    from reyn.plugins.body_read import read_plugin_body_bytes
    from reyn.plugins.skill_load import load_skill_body

    raw_bytes = read_builtin_body_bytes(path)
    if raw_bytes is None:
        raw_bytes = read_plugin_body_bytes(path)
    if raw_bytes is not None:
        content = raw_bytes.decode("utf-8", errors="replace")
    else:
        p = Path(path)
        if not p.is_absolute():
            p = project_dir / p
        content = p.read_text(encoding="utf-8")

    return load_skill_body(
        content, skill_path=path, project_dir=project_dir, alias_claude=True,
    )


# ── discovery / errors (Axis 5, Axis 6) ─────────────────────────────────────


def invocable_skill_names(entries: "Sequence[SkillEntry] | None") -> list[str]:
    """Names reachable via `:` — every registered, non-hidden entry (`menu` +
    `on_demand`; `hidden` reaches no surface at all, same rule `skill_list`
    enforces). Sorted for stable display in errors / `:list`."""
    from reyn.data.skills.registry import VISIBILITY_HIDDEN
    names = [
        e.name for e in (entries or [])
        if getattr(e, "enabled", True)
        and getattr(e, "visibility", "menu") != VISIBILITY_HIDDEN
    ]
    return sorted(names)


def suggest_unknown_skill(name: str, *, known_names: "list[str]") -> list[str]:
    """Up to 3 closest-match suggestions for a typo'd `:name` — mirrors
    ``reyn.interfaces.slash.suggest_for_unknown`` exactly (prefix matches
    first, then fuzzy, then alphabetical fallback), so the two namespaces'
    error UX is consistent. Pure (no I/O)."""
    seen: set[str] = set()
    out: list[str] = []
    if name:
        for n in known_names:
            if n.startswith(name) and n not in seen:
                seen.add(n)
                out.append(n)
    for n in difflib.get_close_matches(name, known_names, n=3, cutoff=0.3):
        if n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        for n in known_names[:3]:
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out[:3]


def skill_invoke_completions(
    prefix: str, entries: "Sequence[SkillEntry] | None",
) -> list[tuple[str, str]]:
    """``(name, description)`` pairs for the `:` inline TUI autocomplete
    (#3100 Axis 6, owner-mandated). Candidates = ``menu`` + ``on_demand``
    skills whose name starts with *prefix*; ``hidden`` never surfaces here,
    matching :func:`invocable_skill_names`. Pure (no I/O), mirrors
    ``reyn.interfaces.slash.slash_command_completions`` in shape."""
    from reyn.data.skills.registry import VISIBILITY_HIDDEN
    out = [
        (e.name, getattr(e, "description", ""))
        for e in (entries or [])
        if getattr(e, "enabled", True)
        and getattr(e, "visibility", "menu") != VISIBILITY_HIDDEN
        and e.name.startswith(prefix)
    ]
    return sorted(out)


__all__ = [
    "SKILL_STACK_MAX",
    "ParsedSkillInvocation",
    "parse_skill_invocation",
    "SkillArgSpec",
    "SkillFrontmatterMeta",
    "read_skill_frontmatter_meta",
    "substitute_arguments",
    "resolve_skill_body",
    "invocable_skill_names",
    "suggest_unknown_skill",
    "skill_invoke_completions",
]
