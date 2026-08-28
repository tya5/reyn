"""Tier 2: #5153 — `reyn permissions` (CLI) moves onto the SAME
`ApprovalLedger` `PermissionResolver` and the web router now use.

Root cause (tui-coder, found while implementing #5153): this command had
its OWN independent `_load`/`_save`, reading/writing `.reyn/approvals.yaml`
directly — a THIRD writer racing the snapshot read-modify-write alongside
`PermissionResolver._persist` and the web router's own equivalent.
Architect ruling (issuecomment-5383838646, scope confirmed
issuecomment-5383848849): ALL THREE doors must share the SAME primitive AND
the SAME rule, or one door breaks what another enforces.

Also closes a pre-existing audit-event gap this command's OLD behavior had
that `_persist` never did: the old `_cmd_revoke`/`_cmd_clear` DELETED the
approval row entirely (`del data[key]`) rather than keeping it with
`approved=False` — the exact "the approval row itself must survive" shape
#5042's own architect ruling established for `_persist`. An append-only
ledger cannot delete a row at all, so this command's revoke/clear now keep
the SAME audit trail `_persist` always has.

Real `ApprovalLedger` + real filesystem paths — no mocks.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.interfaces.cli.commands.permissions import _cmd_clear, _cmd_list, _cmd_revoke
from reyn.security.permissions.approval_ledger import ApprovalLedger
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _ledger(tmp_path: Path) -> ApprovalLedger:
    return ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl")


def test_list_reads_from_the_ledger(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: `reyn permissions list` folds the ledger, not the legacy
    approvals.yaml snapshot -- the visible surface of the storage move."""
    monkeypatch.chdir(tmp_path)
    _ledger(tmp_path).append_approval("actor/file.write/some_dir/", True)

    _cmd_list(Namespace())

    out = capsys.readouterr().out
    assert "some_dir" in out
    assert "Total: 1 entries" in out


def test_revoke_appends_a_false_record_the_row_survives(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: the row must SURVIVE the revoke (as `approved=False`), not
    disappear -- the old CLI behavior's own audit gap, closed here as a
    structural consequence of the ledger being append-only."""
    monkeypatch.chdir(tmp_path)
    key = "actor/file.write/some_dir/"
    _ledger(tmp_path).append_approval(key, True)

    _cmd_revoke(Namespace(key=key))

    out = capsys.readouterr().out
    assert f"Revoked {key!r}" in out
    saved, _bound, _scopes = _ledger(tmp_path).fold()
    assert key in saved, "the approval row must survive a revoke, not disappear"
    assert saved[key] is False


def test_revoke_of_an_unknown_key_exits_nonzero(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: revoking a key with no saved approval at all is a user
    error (typo, wrong key) -- unchanged behavior from the pre-#5153
    snapshot-backed command, now backed by the ledger's own fold."""
    monkeypatch.chdir(tmp_path)
    try:
        _cmd_revoke(Namespace(key="actor/file.write/never_approved/"))
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code != 0
    assert raised, "revoking a key with no saved approval must exit non-zero"


def test_clear_revokes_every_currently_approved_key(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: `reyn permissions clear` appends a revoke record for every
    CURRENTLY-approved key -- the append-only analogue of the old
    truncate-the-whole-file behavior, but auditable (every row survives)."""
    monkeypatch.chdir(tmp_path)
    ledger = _ledger(tmp_path)
    ledger.append_approval("actor/file.write/a/", True)
    ledger.append_approval("actor/file.write/b/", True)

    _cmd_clear(Namespace(yes=True))

    out = capsys.readouterr().out
    assert "Cleared 2 approval(s)" in out
    saved, _bound, _scopes = ledger.fold()
    assert saved == {"actor/file.write/a/": False, "actor/file.write/b/": False}


def test_clear_does_not_touch_an_already_revoked_key_or_prompt(
    tmp_path: Path, monkeypatch, capsys,
):
    """Tier 2: an already-``False`` key is not re-appended -- ``clear``
    only revokes what is CURRENTLY approved."""
    monkeypatch.chdir(tmp_path)
    ledger = _ledger(tmp_path)
    ledger.append_approval("actor/file.write/already_revoked/", False)
    ledger.append_approval("actor/file.write/live/", True)

    _cmd_clear(Namespace(yes=True))

    out = capsys.readouterr().out
    assert "Cleared 1 approval(s)" in out


def test_cli_revoke_clears_the_bound_identity_a_live_resolver_would_have_held(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: #5153 acceptance ⑤ — the cross-writer witness architect
    explicitly asked to be MEASURED, not assumed (issuecomment-5383848849):
    a CLI revoke must not leave a stale bound-identity record behind for
    the SAME "a name is not an identity" reason #5042/#5157 already
    established. `ApprovalLedger.fold()`'s own rule (any `approved=False`
    record clears that key's bound identity, from WHICHEVER writer
    produced it) is what closes this generically -- verified here by
    reading through a FRESH `PermissionResolver` (the process-boundary
    analogue: a resolver that never saw the CLI's own in-process state,
    only the ledger it wrote to)."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "some_dir"
    target.mkdir()
    key = "actor/file.write/some_dir/"

    # A live resolver binds the identity (simulating the server process
    # that originally approved and used this grant).
    resolver = PermissionResolver({}, project_root=tmp_path)
    resolver._saved[key] = True
    resolver._persist(key, True)
    import asyncio
    asyncio.run(
        resolver.require_file_write(PermissionDecl(), str(target / "f.txt"), "actor"),
    )
    assert resolver.bound_identity_get(key) is not None

    # The CLI revokes it -- a SEPARATE process/door, no shared in-memory
    # state with `resolver` above.
    _cmd_revoke(Namespace(key=key))

    # A FRESH resolver (the process-boundary analogue) must see NO bound
    # identity for this key.
    fresh = PermissionResolver({}, project_root=tmp_path)
    assert fresh.bound_identity_get(key) is None, (
        "a CLI revoke must clear the bound identity too, not just the "
        "approval row -- otherwise a later re-approval of the same key "
        "could inherit a stale identity from before the revoke"
    )
