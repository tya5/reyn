"""Tier 2: #5042 — a PATH-flavor approval is bound to the specific
directory/file it approved, not just its NAME, so recreating a same-named
target after the approved one is deleted asks again instead of silently
inheriting the old grant.

Root cause (lead-coder, #5042's own observation, 2026-08-22): after
`reyn-self-work` was deleted, its own 2 approval rows survived in
``.reyn/approvals.yaml`` (``true``, unchanged). Deleting the rows makes the
audit-event "this was once approved" disappear (an audit hole); keeping
them means a LATER tree created at the same path silently inherits the OLD
approval — a real security hole, not hypothetical (an attacker or an
accident recreating the same path gets access without ever being asked).

Architect ruling (issuecomment-5383453175): neither delete-the-row nor
leave-it-as-a-lie. A THIRD option — keep the row (band: audit-events is
satisfied, the "this was approved" fact never disappears), but check the
approved PATH's own identity (`(st_ino, st_birthtime)`, #5084's own
discriminator) at the moment of USE, not at approval time (bind-ON-FIRST-
USE: an approval can predate the target existing at all — binding at
approval time would force an extra prompt the first time the path shows
up). This is the SAME "a name is not an identity" class as #5084 (spawn
lineage) and #5146 (operator driver token) — measured (architect, reading
a real ``reyn-self/.reyn/approvals.yaml``) to apply ONLY to the PATH
flavor of approval key (`<actor>/file.read|file.write/<path>`) — a HOST
key (`<actor>/http.get/<host>`) or a bare PLUGIN key (`mcp.<name>`) has no
"target disappeared" concept at all, so #5042's own fix must not touch
those flavors' own stored shape (architect: "検査した面 ≠ 効く面" — a
uniform 3-value scheme would add unused state to flavors that never need
it).

Storage: the approval ROW itself (`<key>: true`) is completely unchanged
— #5042's own audit-events acceptance. The bound identity for a path-
flavor key lives in a SIBLING top-level ``_bound_identities`` mapping in
the SAME approvals.yaml, keyed by the SAME approval key — never mixed
into the row's own value (see ``PermissionResolver.__bound_identities``'s
own docstring for why: keeping it separate means a pre-#5042 approvals.yaml
needs no migration at all — an entry with no sibling binding is simply
"unbound", read identically whether it predates #5042 or is a fresh grant
whose target does not exist yet).

Real ``PermissionResolver`` + real filesystem paths (real ``mkdir``/
``shutil.rmtree``) — no mocks, mirroring
``test_4204_bucket_b_root_divergence_asks_again.py``'s own harness and its
``resolver._saved[key] = True`` seeding convention (an existing, accepted
direct-poke pattern in this test file's own sibling for the SAME class).
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from reyn.security.permissions import PermissionDecl
from reyn.security.permissions.approval_ledger import ApprovalLedger
from reyn.security.permissions.permissions import PermissionResolver


def _make_resolver(tmp_path) -> PermissionResolver:
    return PermissionResolver({}, project_root=tmp_path)


def _fold(resolver: PermissionResolver):
    """#5431: read (approvals, bound_identities, scopes) via a fresh
    `ApprovalLedger.fold()` — the same production surface `reyn permissions
    list` / `GET /api/permissions` use (migrating a legacy `approvals.yaml`
    snapshot first, exactly like those two `_load()` functions, so a
    legacy-only fixture is still visible), and the surface
    `test_binding_writes_durably_no_tmp_file_ever_used` (below, unchanged
    by this PR) already used directly for the SAME data — rather than the
    removed `saved_get`/`bound_identity_get` accessors."""
    from reyn.security.permissions.approval_ledger import migrate_legacy_snapshot

    ledger = ApprovalLedger(resolver.approval_ledger_path)
    # `resolver.project_root` is the public read; the legacy-snapshot
    # relative path is the same fixed constant `interfaces/{cli,web}/
    # .../permissions.py`'s own `_legacy_snapshot_path()` helpers use.
    migrate_legacy_snapshot(ledger, resolver.project_root / ".reyn" / "approvals.yaml")
    return ledger.fold()


def test_purge_then_recreate_the_same_named_target_asks_again(tmp_path) -> None:
    """Tier 2: acceptance ① — the main one. Approve a dir, use it once
    (binds its identity), delete it, recreate a genuinely NEW dir at the
    same path — the old approval no longer applies; the write falls
    through to a non-interactive deny (``bus=None``, matching #4204's own
    "ask again degrades to PermissionError outside an interactive
    session" contract) rather than silently succeeding."""
    target = tmp_path / "some_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    resolver._saved["actor/file.write/some_dir/"] = True

    write_path = str(target / "f.txt")
    asyncio.run(resolver.require_file_write(PermissionDecl(), write_path, "actor"))

    shutil.rmtree(target)
    target.mkdir()  # a genuinely NEW directory at the same path -- new inode

    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )


def test_the_approval_row_itself_survives_the_stale_mismatch(tmp_path) -> None:
    """Tier 2: acceptance ② — #5042's own audit-events half. After ①'s
    mismatch, the approval row is STILL `true` on disk — "this was once
    approved" never disappears, only stops applying to the new object at
    the same path."""
    target = tmp_path / "some_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    key = "actor/file.write/some_dir/"
    resolver._saved[key] = True
    resolver._persist(key, True)  # actually write it to disk, not just in-memory

    write_path = str(target / "f.txt")
    asyncio.run(resolver.require_file_write(PermissionDecl(), write_path, "actor"))

    shutil.rmtree(target)
    target.mkdir()
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )

    # A FRESH resolver (forces a real re-read from disk) still sees the
    # approval row as true.
    fresh = _make_resolver(tmp_path)
    saved, _bound, _scopes = _fold(fresh)
    assert saved[key] is True, (
        "the approval row disappeared from approvals.yaml -- #5042's own "
        "audit-events half (the row must never vanish) is not satisfied"
    )


def test_host_and_plugin_approvals_are_never_bound_to_an_identity(tmp_path) -> None:
    """Tier 2: acceptance ③ — a flavor-crossing witness. A HOST approval
    (`http.get`) and a PLUGIN approval (bare key, no actor/kind prefix)
    are completely unaffected by #5042's own identity-binding machinery —
    #5042 applies ONLY to the path flavor. Exercises an UNRELATED path-
    flavor purge/recreate cycle in the SAME resolver to prove the host/
    plugin entries stay untouched even while path-flavor binding is
    actively happening elsewhere in the same approvals store."""
    target = tmp_path / "some_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    resolver._saved["actor/file.write/some_dir/"] = True
    # #5431: these two are persisted for real (`_persist`, the production
    # write path) rather than poked directly into `resolver._saved`, so
    # the ledger-fold read below actually finds them.
    resolver._persist("actor/http.get/example.com", True)
    resolver._persist("mcp.broker", True)

    write_path = str(target / "f.txt")
    asyncio.run(resolver.require_file_write(PermissionDecl(), write_path, "actor"))

    saved, bound, _scopes = _fold(resolver)
    assert saved["actor/http.get/example.com"] is True
    assert saved["mcp.broker"] is True
    assert "actor/http.get/example.com" not in bound, (
        "a HOST-flavor approval must never get a bound identity -- "
        "'target disappeared' has no meaning for a host"
    )
    assert "mcp.broker" not in bound, (
        "a PLUGIN-flavor approval must never get a bound identity"
    )


@pytest.mark.asyncio
async def test_an_approval_for_a_not_yet_existing_target_passes_on_first_use(
    tmp_path,
) -> None:
    """Tier 2: acceptance ④ — an approval can predate its target existing
    at all (architect: binding AT approval time would force an extra
    prompt the first time the path shows up). Checking it before the
    target exists must still pass (nothing to bind yet); once the target
    is created, the NEXT check both passes AND performs the actual
    binding (verified via a fresh `ApprovalLedger.fold()`, the genuine
    production surface for this data — #5431 removed the
    `bound_identity_get` accessor, whose only callers were tests)."""
    target = tmp_path / "not_yet"
    resolver = _make_resolver(tmp_path)
    key = "actor/file.write/not_yet/"
    resolver._saved[key] = True
    write_path = str(target / "f.txt")

    # The target does not exist yet -- still honored, no re-ask.
    # require_file_write is a pure permission GATE (raises PermissionError
    # or returns None) -- it performs no filesystem I/O of its own, so
    # calling it against a not-yet-existing target is safe and exercises
    # the SAME public seam every real caller uses, not a private method.
    _saved, bound, _scopes = _fold(resolver)
    assert key not in bound
    await resolver.require_file_write(PermissionDecl(), write_path, "actor")
    _saved, bound, _scopes = _fold(resolver)
    assert key not in bound, (
        "a not-yet-existing target must not be bound to anything -- "
        "there is nothing real to bind to yet"
    )

    target.mkdir()
    await resolver.require_file_write(PermissionDecl(), write_path, "actor")
    _saved, bound, _scopes = _fold(resolver)
    assert key in bound, (
        "the first check AFTER the target started existing must have "
        "bound its identity"
    )


def test_a_legacy_bare_true_entry_is_treated_as_unbound_and_bound_on_first_use(
    tmp_path,
) -> None:
    """Tier 2: acceptance ⑤ — a pre-#5042 approvals.yaml (bare `true`, no
    `_bound_identities` section at all) needs no migration: an entry with
    no sibling binding reads identically to a fresh grant that has never
    been used. First use binds it; a later purge+recreate on the SAME
    entry then asks again, exactly like ①."""
    target = tmp_path / "legacy_dir"
    target.mkdir()
    approvals_dir = tmp_path / ".reyn"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    key = "actor/file.write/legacy_dir/"
    # A hand-written approvals.yaml with NO _bound_identities section at
    # all -- the literal pre-#5042 file shape.
    (approvals_dir / "approvals.yaml").write_text(f"{key}: true\n", encoding="utf-8")

    resolver = _make_resolver(tmp_path)
    saved, bound, _scopes = _fold(resolver)
    assert saved[key] is True
    assert key not in bound, (
        "a legacy entry must read as unbound before its first use"
    )

    write_path = str(target / "f.txt")
    asyncio.run(resolver.require_file_write(PermissionDecl(), write_path, "actor"))
    _saved, bound, _scopes = _fold(resolver)
    assert key in bound, (
        "the legacy entry's first use must have bound it"
    )

    shutil.rmtree(target)
    target.mkdir()
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )


def test_same_session_repeat_use_of_a_birthtime_less_approval_is_not_asked_again(
    tmp_path,
) -> None:
    """Tier 2: #5152 architect ruling (issuecomment-5383604927 —
    RETRACTING this test's own earlier form, issuecomment-5383544769's
    literal fail-closed). That retracted ruling made every path approval
    effectively single-use on a platform without ``st_birthtime``:
    measured regression (tui-coder, PR #5152 CI) — 6 unrelated tests
    failed, every one an ordinary same-session repeat use of an
    already-bound approval with NO purge anywhere; the retracted fix's
    own log line fired on that second, unrelated use.

    Simulated here (this dev machine's filesystem DOES expose
    ``st_birthtime``) by stubbing ``_path_identity`` to strip it, the
    same shape a birthtime-less platform produces. The fd this PR now
    acquires at bind-time anchors every later use for the rest of this
    process's life — the second and third use here must NOT ask again,
    and must NOT be counted as unconfirmable (only a genuinely
    fd-unprotected first-use-after-restart is ③; see
    ``test_first_use_after_a_process_restart_...`` below)."""
    target = tmp_path / "linux_like_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    key = "actor/file.write/linux_like_dir/"
    resolver._saved[key] = True

    real_path_identity = PermissionResolver._path_identity

    def _ino_only(self, path):
        result = real_path_identity(self, path)
        if result is None:
            return None
        return (result[0], None)  # st_birthtime unavailable, ino-only

    write_path = str(target / "f.txt")

    original = PermissionResolver._path_identity
    try:
        PermissionResolver._path_identity = _ino_only
        # First use: binds, and (per #5152) acquires the identity fd.
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )
        before = resolver.unconfirmable_identity_check_count()

        # Second and third use, same session, no purge: fd-anchored --
        # must NOT ask again, must NOT count as unconfirmable.
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )
        assert resolver.unconfirmable_identity_check_count() == before
    finally:
        PermissionResolver._path_identity = original


def test_fd_anchored_identity_still_catches_a_purge_without_birthtime(tmp_path) -> None:
    """Tier 2: #5152's actual fix for the Linux CI-red root cause —
    :meth:`PermissionResolver._acquire_identity_fd` holds an fd on the
    approved path open since bind-time; POSIX guarantees that fd's inode
    cannot be silently handed to a replacement object, so a
    ``rmtree``+``mkdir`` at the same path is STILL caught even with
    ``st_birthtime`` unavailable — the fd is the real confirmation
    mechanism now, not birthtime. This is the case the original #5042
    fix existed to catch, now made to work on the platform (Linux) where
    the naive ino-only comparison was defeated by inode reuse."""
    target = tmp_path / "linux_like_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    key = "actor/file.write/linux_like_dir/"
    resolver._saved[key] = True

    real_path_identity = PermissionResolver._path_identity

    def _ino_only(self, path):
        result = real_path_identity(self, path)
        if result is None:
            return None
        return (result[0], None)  # st_birthtime unavailable, ino-only

    write_path = str(target / "f.txt")

    original = PermissionResolver._path_identity
    try:
        PermissionResolver._path_identity = _ino_only
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )

        shutil.rmtree(target)
        target.mkdir()  # a genuinely NEW directory at the same path
        with pytest.raises(PermissionError):
            asyncio.run(
                resolver.require_file_write(PermissionDecl(), write_path, "actor")
            )
    finally:
        PermissionResolver._path_identity = original


def test_first_use_after_a_process_restart_honors_the_grant_and_counts_as_unconfirmable(
    tmp_path,
) -> None:
    """Tier 2: acceptance ③ — the ONE window #5152's fd-anchoring does
    NOT close: the first use after a process restart has no fd yet (a
    fresh process's fd table starts empty), so on a birthtime-less
    platform it genuinely cannot be confirmed either way. The ruling
    (issuecomment-5383604927): honor the grant — never silently deny,
    that was the retracted ruling's own mistake — and make the fact
    observable via ``unconfirmable_identity_check_count``.

    Simulated by a SECOND, independent ``PermissionResolver`` instance
    reading the SAME ``approvals.yaml`` — the natural analogue of a
    process restart: memory (and the fd table with it) starts empty,
    only the persisted ``(ino, birthtime)`` survives."""
    target = tmp_path / "linux_like_dir"
    target.mkdir()
    key = "actor/file.write/linux_like_dir/"

    real_path_identity = PermissionResolver._path_identity

    def _ino_only(self, path):
        result = real_path_identity(self, path)
        if result is None:
            return None
        return (result[0], None)  # st_birthtime unavailable, ino-only

    write_path = str(target / "f.txt")

    original = PermissionResolver._path_identity
    try:
        PermissionResolver._path_identity = _ino_only

        first = _make_resolver(tmp_path)
        first._saved[key] = True
        first._persist(key, True)
        asyncio.run(
            first.require_file_write(PermissionDecl(), write_path, "actor")
        )

        # A fresh resolver instance == the process-restart analogue.
        second = _make_resolver(tmp_path)
        before = second.unconfirmable_identity_check_count()
        asyncio.run(
            second.require_file_write(PermissionDecl(), write_path, "actor")
        )
        assert second.unconfirmable_identity_check_count() == before + 1
    finally:
        PermissionResolver._path_identity = original


@pytest.mark.asyncio
async def test_binding_writes_durably_no_tmp_file_ever_used(tmp_path) -> None:
    """Tier 2: architect co-vet (issuecomment-5383499299) — binding fires
    on every path-approval's FIRST USE, inside ordinary tool execution,
    not just when a human approves (rare) like `_persist`'s own decision.
    Originally fixed via tmp-file + atomic `replace` (the same precedent
    `AgentIdentityGenerationStore.record` uses); #5153 (architect ruling,
    issuecomment-5383838646) replaced that snapshot read-modify-write
    (durable against a mid-write CRASH, but not against a concurrent
    WRITER) with an APPEND to the `approvals.jsonl` ledger — see
    `approval_ledger.py`. This pins the OBSERVABLE consequence of THAT
    mechanism: no `.tmp` file is EVER created for a bind (there is
    nothing to replace — the write is a single small ``fsync``'d
    append), and folding the ledger correctly reflects the bound key's
    value afterward."""
    target = tmp_path / "atomic_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    key = "actor/file.write/atomic_dir/"
    resolver._saved[key] = True
    resolver._persist(key, True)

    write_path = str(target / "f.txt")
    await resolver.require_file_write(PermissionDecl(), write_path, "actor")

    ledger_path = tmp_path / ".reyn" / "approvals.jsonl"
    tmp_sibling = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    assert not tmp_sibling.exists(), (
        "a .tmp sibling exists -- the append-only ledger should never "
        "create one at all"
    )
    from reyn.security.permissions.approval_ledger import ApprovalLedger
    saved, bound, _scopes = ApprovalLedger(ledger_path).fold()
    assert saved[key] is True
    assert bound[key][0] is not None
