"""The declared (NOT effective) write-path scope for `describe_session`
(#5012-A).

Reports what `sandbox.policy` DECLARES about writable paths, never the
resolved/enforced scope — architect ruling, #5012-A: `resolve_sandbox_policy()`
needs a caller-supplied `write_paths` floor ("this op needs this directory")
that a context-free tool cannot know and must not invent a stand-in for
(the same reasoning `doctor.py`'s C-5 check already documents, `#4364`).
Returning a resolved/effective value here would be exactly the fabrication
CLAUDE.md's own doc-sync and #5009 vocabulary spend this whole session
fighting: a value presented without declaring what it actually represents.

Two-value discriminator (architect ruling, #5012-A — the SAME shape #5009's
`*_reported` gates use): "no `sandbox.policy` at all" and "`sandbox.policy`
set but the write-scope keys are absent/empty" are DIFFERENT states, and
returning `[]` for both would conflate them into a single false claim
("nowhere is writable") — this repo's own `sandbox.policy` IS currently
undeclared (confirmed, #5012-A), so this distinction is not a hypothetical
edge case, it is the live default.
"""
from __future__ import annotations

from typing import Any


def describe_write_scope(sandbox_config: Any) -> dict:
    """*sandbox_config*: the `ReynConfig.sandbox` object (a `SandboxConfig`
    or any object exposing a `.policy` attribute — kept duck-typed rather
    than importing `SandboxConfig` to avoid a config-package dependency
    this module does not otherwise need).

    Returns one of three shapes, discriminated by ``"declared"``:
    - ``{"declared": False}`` — no ``sandbox.policy`` block at all. This is
      NOT the same as "unrestricted" (see module docstring) — an op's own
      workspace floor still governs; this tool cannot say what that floor
      is without an op context.
    - ``{"declared": True, "allow_write_paths": None, "deny_write_paths": None}``
      — a ``sandbox.policy`` block exists but neither write-scope key
      appears in it.
    - ``{"declared": True, "allow_write_paths": [...], "deny_write_paths": [...]}``
      — the block's own declared values, verbatim (a key absent from the
      config is reported as ``None``, not ``[]``, preserving the same
      absent-vs-empty distinction one level down)."""
    declared_policy = getattr(sandbox_config, "policy", None)
    if declared_policy is None:
        # `is None`, not truthiness: `sandbox.policy: {}` (an explicit,
        # empty block) is a DIFFERENT state from `sandbox.policy` never
        # appearing at all — collapsing them via `if not declared_policy`
        # would repeat the exact conflation this function exists to avoid.
        return {"declared": False}
    return {
        "declared": True,
        "allow_write_paths": declared_policy.get("allow_write_paths"),
        "deny_write_paths": declared_policy.get("deny_write_paths"),
    }
