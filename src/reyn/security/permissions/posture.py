"""FP-0069 §2 — the permission posture dial (#5825 stage 1).

``permissions.mode`` in ``reyn.yaml`` (overridable in ``reyn.local.yaml``,
and at runtime), named by DISPOSITION — what an actor may do — not by
rank. Owner-ratified 2026-09-06 (the proposal's own §10 records the three
verbatim rulings): ``docs/deep-dives/proposals/0069-permission-posture-dial.md``.

**Stage 1 scope, deliberately narrow** (lead-coder dispatch, #5825): the
config key, the 3 values this stage accepts, their total order, and the
"remove ``unbounded`` by config" seam. Wiring a mode's VALUE into actual
enforcement (a capability denied by a restrict layer staying denied in
every mode; ``bounded``'s own boundary replacing the prompt) is later
stages' work, not this module's.

**``bounded`` is a 4th value the doc already names (§2) but this stage
does NOT accept** — it "Requires §6" (the doc's own table, verbatim): a
``sandboxed_exec``-owned restrictive policy that does not exist yet
(#5825 stage 2). Accepting the STRING today without the boundary behind
it would ship a name that does not protect what it claims to — the exact
"declared, never reached" shape #5849/#5862 already closed elsewhere in
this same file's sibling registry. :func:`parse_permission_mode` refuses
it with a message naming stage 2, rather than silently aliasing it to
``ask`` — an alias would be worse than refusing: a config author who
wrote ``bounded`` believing it meant a real boundary would read `ask`'s
behavior as satisfying that belief.
"""
from __future__ import annotations

import enum
import logging

logger = logging.getLogger(__name__)


class PermissionMode(str, enum.Enum):
    """The 3 values #5825 stage 1 accepts (doc §2's own 4, minus
    ``bounded`` — see module docstring). String-valued so a raw config
    string compares equal to a member directly (``raw == PermissionMode.ASK``),
    the same convenience :class:`~reyn.config_axis.Axis` already uses."""

    READ_ONLY = "read_only"
    ASK = "ask"
    UNBOUNDED = "unbounded"


#: Strict-to-permissive, total (doc §2: "Ordering is total and strict-to-
#: permissive, so 'at least as strict as X' is expressible") — the single
#: source both :func:`rank` and any future comparison read, never a second
#: hand-written order that could drift from this one.
PERMISSION_MODE_ORDER: "tuple[PermissionMode, ...]" = (
    PermissionMode.READ_ONLY,
    PermissionMode.ASK,
    PermissionMode.UNBOUNDED,
)

#: doc §7 (Migration): "Existing projects keep working with mode: ask" —
#: the ONE place this default is stated, per #5825 stage 1's acceptance
#: criterion ("no silent default, or has a default stated in one place a
#: test reads"). Byte-identical to pre-dial behavior: no ``permissions.mode``
#: key at all resolves to exactly what an unconfigured project already did.
DEFAULT_PERMISSION_MODE: PermissionMode = PermissionMode.ASK

#: The doc's own 4th value, named explicitly here (not just absent from
#: :class:`PermissionMode`) so :func:`parse_permission_mode` can give it a
#: distinct, honest error rather than folding it into the generic
#: "unrecognized value" message a genuine typo gets.
_BOUNDED_NOT_YET_IMPLEMENTED = "bounded"


class PermissionModeError(ValueError):
    """A ``permissions.mode`` value this stage cannot honor. Base class for
    both the generic-unrecognized and the ``bounded``-specific case below,
    so a caller that only wants "refuse startup, log the reason" can catch
    this one type and still get the more specific message in ``str(exc)``."""


class BoundedModeNotImplementedError(PermissionModeError):
    """``mode: bounded`` was configured — a real, doc-named value (§2),
    just not one this stage accepts (see module docstring). Distinct from
    :class:`PermissionModeError`'s generic case so a caller wanting to
    special-case "not yet, not a typo" can — e.g. pointing an operator at
    the tracking issue instead of `reyn config fields`."""


def parse_permission_mode(raw: str) -> PermissionMode:
    """Parse *raw* (a ``permissions.mode`` config value) into a
    :class:`PermissionMode`.

    Raises :class:`BoundedModeNotImplementedError` for ``"bounded"``
    specifically (never silently aliased to :data:`PermissionMode.ASK` —
    see module docstring for why an alias would be worse than a refusal),
    and the base :class:`PermissionModeError` for any other value that
    matches none of the 3 stage-1 members (a typo, or a value that never
    existed)."""
    if raw == _BOUNDED_NOT_YET_IMPLEMENTED:
        raise BoundedModeNotImplementedError(
            "permissions.mode: 'bounded' is not yet implemented (#5825 "
            "stage 2 — it requires a sandboxed_exec-owned restrictive "
            "policy that does not exist yet). Use 'read_only', 'ask', or "
            "'unbounded' for now."
        )
    try:
        return PermissionMode(raw)
    except ValueError:
        valid = ", ".join(repr(m.value) for m in PERMISSION_MODE_ORDER)
        raise PermissionModeError(
            f"permissions.mode: {raw!r} is not a recognized value. "
            f"Valid values: {valid}."
        ) from None


def rank(mode: PermissionMode) -> int:
    """*mode*'s position in :data:`PERMISSION_MODE_ORDER` — lower is
    stricter. The one place a caller should compare two modes' relative
    strictness; never compare `.value` strings or re-derive an order."""
    return PERMISSION_MODE_ORDER.index(mode)


def is_at_least_as_strict(mode: PermissionMode, floor: PermissionMode) -> bool:
    """``True`` iff *mode* is *floor* or stricter — doc §2's own
    "'at least as strict as X' is expressible" acceptance line, as a
    direct callable rather than an inline rank comparison at every call
    site (the seam a future enforcement stage will actually call)."""
    return rank(mode) <= rank(floor)


def resolve_permission_mode(
    raw: "str | None", *, unbounded_disabled: bool,
) -> PermissionMode:
    """The one function a config-reading caller should use to get the
    EFFECTIVE :class:`PermissionMode` — *raw* is the merged
    ``permissions.mode`` config value (``None`` when unset, resolving to
    :data:`DEFAULT_PERMISSION_MODE`), *unbounded_disabled* is the merged
    ``permissions.disable_unbounded_mode`` flag (see
    :func:`~reyn.config.loader._merge`'s sticky-OR handling of that one
    key — never a plain last-tier-wins merge, doc §8: "a session cannot
    re-enable it").

    A configured ``unbounded`` while disabled does NOT raise — a locked-
    down fleet's config should not crash an operator's session; it falls
    back to :data:`DEFAULT_PERMISSION_MODE` (the safe, byte-identical-to-
    today value) with a WARNING naming what was refused and why, so the
    drift is visible rather than silent (CLAUDE.md band: observability).
    A genuinely unrecognized value (a typo, or `bounded`) still raises —
    unlike a disabled-but-otherwise-valid `unbounded`, there is no safe
    value to substitute for a value that was never a real mode at all."""
    mode = (
        DEFAULT_PERMISSION_MODE if raw is None else parse_permission_mode(raw)
    )
    if mode is PermissionMode.UNBOUNDED and unbounded_disabled:
        logger.warning(
            "permissions.mode: 'unbounded' was configured but is disabled "
            "by permissions.disable_unbounded_mode (set by this project or "
            "a higher config tier) — falling back to %r.",
            DEFAULT_PERMISSION_MODE.value,
        )
        return DEFAULT_PERMISSION_MODE
    return mode
