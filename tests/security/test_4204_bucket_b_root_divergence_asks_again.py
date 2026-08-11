"""Tier 2: #4204 bucket B — PermissionResolver's two roots (project_root,
file_zone_root) answer different questions BY DESIGN, not a defect.

Architect's ruling (2026-08-11, #4204), confirmed by lead-coder: this is a
RECORD, not a fix.

  - ``project_root`` anchors the persisted-approval LEDGER (approvals.yaml
    is project-scoped under ``.reyn/`` — #2415: resolving it CWD-relative
    was the bug that #2415 itself fixed; anchoring it on ``project_root``
    is necessary, not incidental).
  - ``file_zone_root`` anchors the agent's default WORK-ZONE (under a
    container backend, #1414, this is the in-container repo root — which
    SHOULD differ from the host-side ``project_root``).

These answer genuinely different questions, so they are correctly allowed
to diverge (#1414's own test file, ``test_permission_file_zone_anchor_
1414.py``, already pins the container-zone-vs-host-approvals split as
intentional). ``_is_path_approved_for`` (permissions.py) resolves BOTH the
query path and the stored approval key against the SAME base
(``project_root``), so that comparison is always internally consistent —
divergence cannot cause an unapproved path to be silently GRANTED (the
"widening" direction architect explicitly ruled out building a repro for:
two frames that are not sub-paths of each other cannot spuriously match).

The one real, production-observable consequence of divergence: a path an
operator approved in one frame (e.g. a host-relative grant recorded via
``project_root``) does not automatically cover the semantically-"same"
path when the agent later asks for it in a DIFFERENT frame (e.g. a
container-mounted path anchored on ``file_zone_root``) — the request
falls through to the ordinary non-interactive deny (``bus=None`` — "ask
again" degrades to a clear ``PermissionError`` outside an interactive
session, mirroring every other JIT-ask call site's documented
bus=None behavior). This file pins exactly that outcome, through the
REAL ``PermissionResolver`` — never a hand-assembled
``EffectivePermission([AgentLayer(decl)])`` with a layer stripped out (a
configuration production never constructs; see CLAUDE.md's #3916
discriminator).

⚠️ Do NOT read this as "the sandbox layer bounds this" — FILE_READ/
FILE_WRITE no longer participate in EffectivePermission's ∩ at all
(#3901 PR-B ③); any containment for those two axes is enforced at the
kernel/sandbox-backend level, a SEPARATE mechanism from what this file
tests. Writing "∩ contains it" here would falsify #3901's owner-decided
layer split.

Unmeasured (recorded here per architect's own disclosure, not silently
dropped): no ACTUAL divergent execution under a real container backend
has been observed — this test constructs the divergence directly via
``PermissionResolver(project_root=..., file_zone_root=...)``, the same
two knobs #1414's own test file exercises. Whether ``_is_config_approved``
(the sibling config-tier approval path) uses the same base as
``_is_path_approved_for`` is also unconfirmed and out of this file's
scope.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.security.permissions import PermissionDecl
from reyn.security.permissions.permissions import PermissionResolver


def test_root_divergence_falls_through_to_deny_not_a_silent_grant(tmp_path) -> None:
    """Tier 2: #4204 bucket B — with project_root != file_zone_root, a path
    approved in the project_root frame does NOT silently cover the
    semantically-equivalent path in the file_zone_root frame; the request
    denies non-interactively (bus=None) rather than being granted through
    either root's interpretation alone.

    Uses require_file_WRITE, not read: the default READ zone is the WHOLE
    file_zone_root tree (#1414's own test file: "read zone = whole repo"),
    so any path under file_zone_root always zone-passes for reads —
    divergence never even reaches the approval fallback on that axis. The
    default WRITE zone is narrower (only ``.reyn/`` under file_zone_root,
    #1414), so a write target outside it is the case that actually
    exercises the approval-vs-zone fallback this file is about.
    """
    host_root = tmp_path / "host_proj"
    container_root = tmp_path / "container_mount"
    host_root.mkdir()
    container_root.mkdir()

    resolver = PermissionResolver(
        {}, project_root=host_root, file_zone_root=container_root,
    )

    # A human previously approved "shared/data.txt", recorded (as
    # approvals.yaml persists it) relative to project_root — the frame the
    # approving human/operator was in.
    resolver._saved["actor/file.write/shared/"] = True

    # The agent, operating in the file_zone_root (container) frame, later
    # asks to write what IS conceptually the same artifact, but as an
    # absolute path anchored on file_zone_root, not project_root.
    container_path = str(container_root / "shared" / "data.txt")

    # Neither root's interpretation alone grants it: it is outside the
    # default write zone (only ``.reyn/`` under file_zone_root is
    # zone-granted — #1414) and the stored approval, resolved against
    # project_root, points at a different absolute tree entirely.
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), container_path, "actor")
        )


def test_approval_recorded_and_queried_in_the_same_project_root_frame_still_works(
    tmp_path,
) -> None:
    """Tier 2: #4204 bucket B — the SAME approval, queried as an absolute
    path that actually resolves under project_root (the frame it was
    recorded in), is honored — proving ``_is_path_approved_for`` stays
    internally consistent (it resolves query path and stored key against
    the SAME base) regardless of what file_zone_root is set to. This is
    the accept-side counterpart: divergence does not break a same-frame
    approval either."""
    host_root = tmp_path / "host_proj"
    container_root = tmp_path / "container_mount"
    host_root.mkdir()
    container_root.mkdir()

    resolver = PermissionResolver(
        {}, project_root=host_root, file_zone_root=container_root,
    )
    resolver._saved["actor/file.write/shared/"] = True
    (host_root / "shared").mkdir()

    host_path = str(host_root / "shared" / "data.txt")

    asyncio.run(
        resolver.require_file_write(PermissionDecl(), host_path, "actor")
    )  # must not raise
