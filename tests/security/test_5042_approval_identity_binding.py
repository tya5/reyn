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
from reyn.security.permissions.permissions import PermissionResolver


def _make_resolver(tmp_path) -> PermissionResolver:
    return PermissionResolver({}, project_root=tmp_path)


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
    assert fresh.saved_get(key) is True, (
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
    resolver._saved["actor/http.get/example.com"] = True
    resolver._saved["mcp.broker"] = True

    write_path = str(target / "f.txt")
    asyncio.run(resolver.require_file_write(PermissionDecl(), write_path, "actor"))

    assert resolver.saved_get("actor/http.get/example.com") is True
    assert resolver.saved_get("mcp.broker") is True
    assert resolver.bound_identity_get("actor/http.get/example.com") is None, (
        "a HOST-flavor approval must never get a bound identity -- "
        "'target disappeared' has no meaning for a host"
    )
    assert resolver.bound_identity_get("mcp.broker") is None, (
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
    binding (verified via the public `bound_identity_get` accessor, not a
    private-state peek)."""
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
    assert resolver.bound_identity_get(key) is None
    await resolver.require_file_write(PermissionDecl(), write_path, "actor")
    assert resolver.bound_identity_get(key) is None, (
        "a not-yet-existing target must not be bound to anything -- "
        "there is nothing real to bind to yet"
    )

    target.mkdir()
    await resolver.require_file_write(PermissionDecl(), write_path, "actor")
    assert resolver.bound_identity_get(key) is not None, (
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
    assert resolver.saved_get(key) is True
    assert resolver.bound_identity_get(key) is None, (
        "a legacy entry must read as unbound before its first use"
    )

    write_path = str(target / "f.txt")
    asyncio.run(resolver.require_file_write(PermissionDecl(), write_path, "actor"))
    assert resolver.bound_identity_get(key) is not None, (
        "the legacy entry's first use must have bound it"
    )

    shutil.rmtree(target)
    target.mkdir()
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), write_path, "actor")
        )


def test_st_birthtime_unavailable_degrades_to_ino_only_like_5084(tmp_path) -> None:
    """Tier 2: the one thing architect asked to be measured, not assumed
    — the same platform-degrade #5084 already documents (most Linux
    filesystems via plain ``stat()`` expose no ``st_birthtime``): the
    identity comparison must still work with ``ino`` alone, never raise,
    on a platform where ``st_birthtime`` is absent. Simulated directly
    (this dev machine's own filesystem DOES expose ``st_birthtime``, so a
    real platform-dependent skip would not exercise the degrade path) by
    stubbing ``_path_identity`` to return an ino-only tuple, the SAME
    shape ``getattr(st, "st_birthtime", None)`` produces on such a
    platform -- ``ino`` alone must still correctly detect a mismatch."""
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
        assert resolver.bound_identity_get(key) == (
            real_path_identity(resolver, target)[0], None,
        )

        shutil.rmtree(target)
        target.mkdir()  # a new inode, ino-only comparison must still catch this
        with pytest.raises(PermissionError):
            asyncio.run(
                resolver.require_file_write(PermissionDecl(), write_path, "actor")
            )
    finally:
        PermissionResolver._path_identity = original


@pytest.mark.asyncio
async def test_binding_writes_atomically_no_tmp_file_left_behind(tmp_path) -> None:
    """Tier 2: architect co-vet (issuecomment-5383499299) — binding now
    fires on every path-approval's FIRST USE, inside ordinary tool
    execution, not just when a human approves (rare) like `_persist`'s
    own read-modify-write. A crash mid-`write_text` would truncate
    `approvals.yaml` and lose EVERY approval, not just the one being
    bound. Fixed via tmp-file + atomic `replace` (the same precedent
    `AgentIdentityGenerationStore.record` already uses) -- this pins the
    OBSERVABLE consequence: no `.tmp` sibling survives a successful bind,
    and the approvals file itself is genuinely valid YAML afterward (a
    half-written file would not parse)."""
    import yaml

    target = tmp_path / "atomic_dir"
    target.mkdir()
    resolver = _make_resolver(tmp_path)
    key = "actor/file.write/atomic_dir/"
    resolver._saved[key] = True
    resolver._persist(key, True)

    write_path = str(target / "f.txt")
    await resolver.require_file_write(PermissionDecl(), write_path, "actor")

    approvals_path = tmp_path / ".reyn" / "approvals.yaml"
    tmp_sibling = approvals_path.with_suffix(approvals_path.suffix + ".tmp")
    assert not tmp_sibling.exists(), (
        "a .tmp sibling survived a successful bind -- the atomic "
        "replace did not clean up after itself"
    )
    data = yaml.safe_load(approvals_path.read_text(encoding="utf-8"))
    assert data[key] is True
    assert data["_bound_identities"][key]["ino"] is not None
