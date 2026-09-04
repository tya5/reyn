"""Shared workspace-boundary resolution — #4206/#5081/#5084.

:func:`within_workspace` is extracted (not duplicated — the #5057 lesson
this arc closed three times in one night: the same guard written
independently in several places is a "list drift" hazard, not defense in
depth) from what was a private closure inside
:meth:`reyn.runtime.session.Session._workspace_base_dir` (#5081) —
lead-coder's own measurement, confirmed by architect: that function was
never a reusable module function, so #5084's own agent-layer
``project_context_path`` override (the SAME "⊆ workspace, restrict-only,
protect-at-use" shape ``base_dir`` already established) would otherwise
have had to duplicate it a second time, and ``AgentRegistry.create()``'s
own write-time ``base_dir`` bound-check (a THIRD independent copy of the
same check) is folded onto this module too.

**Relative-value resolution is NOT a new rule this module invents.**
#5084's own cwd-anchor fix (architect's own finding, #2415 family: a
hand-written ``profile.yaml``'s relative ``base_dir`` resolved against
``Path.cwd()`` at read time while ``registry.create()``'s write path
resolved it against the project root — the same key meaning something
different depending on who wrote it) routes through the EXISTING
``${REYN_PROJECT_DIR}`` token vocabulary
(:mod:`reyn.plugins.tokens`, ADR-0064 §3.4-3.6) instead of a
bespoke "bare relative path resolves against workspace root" convention —
architect's own self-correction, after finding that vocabulary already
declared and already expanded fresh on every read (never baked into a
persisted copy, #3629's own "dynamic param" discipline, which already
solved the "an absolute path must not calcify into history" problem this
fix would otherwise have had to solve a second time). A caller that wants
a workspace-relative value writes ``${REYN_PROJECT_DIR}/repos/<name>``,
the SAME token a skill/plugin author already uses — a bare relative
string with no token is REJECTED outright (logged, treated as no
override — see ``Session._read_base_dir_override``'s own docstring for
the exact rule and its rationale), never silently reinterpreted as
either workspace-relative (option (a), the design this module's own
first draft implemented, then withdrawn) or as relative to the reyn
process's current working directory (option (b), "leave it alone" — the
ORIGINAL bug, and this sentence's own earlier, now-corrected claim: an
untouched bare relative string is not actually a null-op, since
``Path.cwd()``-relative resolution IS a behavior, just a
cwd-dependent one). Architect's own measure of the choice: "which of the
two remaining options fails silently" (issuecomment-5378958683) —
rejection never does.

**Two distinct mechanisms deliberately share the SAME token name.**
``${REYN_PROJECT_DIR}`` (this module's own consumer, "A": values reyn
itself reads — ``profile.yaml``'s ``base_dir`` (``project_context_path``,
this same shape, was retired in #5742 PR2), ``permissions``' file paths)
expands through
:func:`reyn.plugins.tokens.expand_with_map`, an in-process string
substitution — never through ``os.environ``. ``REYN_PROJECT_DIR`` (no
``${}``, no lowercase collision intended) is ALSO the name of an actual
environment variable #5084's hook-derivation slice (④) exports for
CHILD PROCESSES (hooks' ``exec``/``exec_capture``, which cannot run this
module's own expansion — they only ever see a real OS environment). Same
name, two layers, never merged — exactly the ``expand_env`` vs
``expand_reyn_tokens`` split ``tokens.py``'s own module docstring already
draws for a different pair of layers; do not conflate a caller reading
this module's token map with a caller reading its own process
environment.
"""
from __future__ import annotations

from pathlib import Path


def within_workspace(candidate: Path, workspace_root: Path) -> bool:
    """True iff *candidate* falls under *workspace_root* — restrict-only,
    the bound every #4206-family agent-layer override (``base_dir`` today;
    ``project_context_path`` used it too before #5742 PR2 retired that
    field) shares.

    *candidate* is resolved here (idempotent if the caller already
    resolved it, e.g. after token expansion); *workspace_root* is resolved
    here too, so either may be passed as given."""
    resolved = candidate.resolve()
    root = workspace_root.resolve()
    return resolved == root or root in resolved.parents


def resolve_base_dir_candidate(
    raw_value: "str | None", *, workspace_root: Path,
) -> "Path | None":
    """#5428: the pure "one candidate → token-expand → boundary-check"
    step, extracted out of
    :meth:`reyn.runtime.session.Session._read_base_dir_override` so a
    caller with NO session layer (``reyn doctor``, #5428's own real
    consumer) can validate an agent-profile ``base_dir:`` candidate
    without duplicating this logic — the exact #5057 "same guard, second
    copy" class this module's own docstring already names.

    Deliberately does NOT decide layer order (session-config vs
    agent-profile vs Agent default) — lead-coder's own #5428 scoping: a
    caller with no session layer would otherwise force a
    session-layer-shaped branch into this function, growing its
    signature for every new caller ("does the argument count grow when
    the caller count grows?" — #5428's own discriminant). Callers own
    their own layer order; this function only ever validates ONE
    already-selected raw string.

    Returns ``None`` for: absent/empty *raw_value*, a bare relative path
    with no ``${REYN_PROJECT_DIR}`` token (rejected outright — never
    silently reinterpreted as workspace-relative or as relative to the
    calling process's own cwd, see this module's own docstring), or a
    value that expands outside *workspace_root* (:func:`within_workspace`).
    Never raises on a malformed value — the caller decides whether/how to
    log a rejection; this function's own contract is validate-or-None,
    not validate-or-raise (a malformed hand-written override must not
    crash construction, #5081)."""
    if not raw_value:
        return None
    from reyn.plugins.tokens import expand_with_map

    root = workspace_root.resolve()
    # Order load-bearing (architect, issuecomment-5378958683): expand the
    # token FIRST, so `${REYN_PROJECT_DIR}/...` is judged by what it
    # expands to, never as a literal string or as "relative".
    expanded = expand_with_map(str(raw_value), {"REYN_PROJECT_DIR": str(root)})
    candidate = Path(expanded)
    if not candidate.is_absolute():
        return None
    if not within_workspace(candidate, root):
        return None
    return candidate


__all__ = ["resolve_base_dir_candidate", "within_workspace"]
