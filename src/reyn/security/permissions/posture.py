"""FP-0069 §2 — the permission posture dial (#5825).

``permissions.mode`` in ``reyn.yaml`` (overridable in ``reyn.local.yaml``,
and at runtime), named by DISPOSITION — what an actor may do — not by
rank. Owner-ratified 2026-09-06 (the proposal's own §10 records the three
verbatim rulings): ``docs/deep-dives/proposals/0069-permission-posture-dial.md``.

**Stage 1** (lead-coder dispatch, #5825): the config key, its (then) 3
values, their total order, and the "remove ``unbounded`` by config"
seam. **Stage 2** (#5825, architect design + lead-coder dispatch) adds
the 4th value, ``bounded`` — doc §6/§6.1/§8. This module still owns only
the VALUE VOCABULARY and its order; the session-level judgment of
whether ``bounded``'s own boundary is actually enforced (and the
downgrade to ``ask`` when it is not — see :mod:`reyn.runtime.session`'s
``resolved_permission_mode``) lives OUTSIDE this module, which has no
I/O and cannot answer that question itself.
"""
from __future__ import annotations

import enum
import logging

logger = logging.getLogger(__name__)


class PermissionMode(str, enum.Enum):
    """The 4 values doc §2 names. String-valued so a raw config string
    compares equal to a member directly (``raw == PermissionMode.ASK``),
    the same convenience :class:`~reyn.config_axis.Axis` already uses."""

    READ_ONLY = "read_only"
    ASK = "ask"
    BOUNDED = "bounded"
    UNBOUNDED = "unbounded"


#: Strict-to-permissive, total (doc §2: "Ordering is total and strict-to-
#: permissive, so 'at least as strict as X' is expressible") — the single
#: source both :func:`rank` and any future comparison read, never a second
#: hand-written order that could drift from this one. ``bounded`` sits
#: between ``ask`` and ``unbounded`` (doc §2's own table order) — #5825
#: stage 2 added it here.
PERMISSION_MODE_ORDER: "tuple[PermissionMode, ...]" = (
    PermissionMode.READ_ONLY,
    PermissionMode.ASK,
    PermissionMode.BOUNDED,
    PermissionMode.UNBOUNDED,
)

#: doc §7 (Migration): "Existing projects keep working with mode: ask" —
#: the ONE place this default is stated, per #5825 stage 1's acceptance
#: criterion ("no silent default, or has a default stated in one place a
#: test reads"). Byte-identical to pre-dial behavior: no ``permissions.mode``
#: key at all resolves to exactly what an unconfigured project already did.
DEFAULT_PERMISSION_MODE: PermissionMode = PermissionMode.ASK


class PermissionModeError(ValueError):
    """A ``permissions.mode`` value that matches no :class:`PermissionMode`
    member — a typo, or a value that never existed at all."""


def parse_permission_mode(raw: str) -> PermissionMode:
    """Parse *raw* (a ``permissions.mode`` config value) into a
    :class:`PermissionMode`, or raise :class:`PermissionModeError` naming
    the valid set. #5825 stage 2: ``bounded`` is a real, ordinary member
    now — parsing it here says only "this is a recognized VALUE"; whether
    its own boundary is actually enforced this session is a SEPARATE,
    session-level judgment this function does not make (see
    :mod:`reyn.runtime.session`'s ``resolved_permission_mode``)."""
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
    today value) with a WARNING naming what was refused and why. That
    WARNING reaches ``reyn.log`` unconditionally, but NOT the operator's
    own screen in the shipped interactive configuration — ``chat.py``'s
    ``_setup_interactive_logging`` only adds a ``StreamHandler`` when
    ``not is_interactive`` (CLAUDE.md's own 2nd of 3 questions: "is this
    visible with the shipped config?" — here, no). Surfacing this
    downgrade on-screen is Session.permission_mode_downgrade_reason's job
    (#5825 stage 2 — the same Ctx-pane row `bounded`'s own network-
    enforcement downgrade now uses), not this function's — this module
    only guarantees the fallback is DURABLY RECORDED, not that an
    operator watching the screen sees it in the moment. A genuinely
    unrecognized value (a typo — `bounded` is a real member now) still
    raises — there is no safe value to substitute for one that was never
    a real mode at all."""
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


def sandbox_mode_for_permission_mode(
    configured_sandbox_mode: str, permission_mode: PermissionMode,
) -> str:
    """#5825 stage 2 (doc §6, lead-coder dispatch: "新しい preset を作らない
    こと ... sandbox.mode: strict の defaults を選ぶだけ"): ``bounded``
    SELECTS the existing ``sandbox.mode: strict`` preset
    (``_SANDBOX_STRICT_MODE_DEFAULTS`` — closed network, denied
    subprocess, empty env allowlist) — no new preset is built. Every
    OTHER :class:`PermissionMode` leaves *configured_sandbox_mode*
    untouched.

    This is a SELECTION only, not the enforcement-gap judgment (whether
    the selected preset's network deny can actually be honored by the
    resolved backend) — that is
    ``reyn.runtime.session.Session.network_enforcement_gap``'s job, which
    calls THIS same function so the value it checks and the value the
    real exec path uses can never diverge (see that property's own
    docstring)."""
    if permission_mode is PermissionMode.BOUNDED:
        return "strict"
    return configured_sandbox_mode
