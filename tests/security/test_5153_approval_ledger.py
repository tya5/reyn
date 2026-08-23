"""Tier 2: #5153 — ``ApprovalLedger`` closes the read-modify-write race
`.reyn/approvals.yaml`'s snapshot persistence had (lead-coder's own
finding: ``_persist`` needed momentary ownership of the WHOLE file to
change even one key, so two writers racing that same shape silently lost
one's update).

Architect ruling (issuecomment-5383838646) and its own explicit acceptance
criteria (lead-coder's relay + architect's own message):
  ① two processes concurrently approving DIFFERENT keys → both survive
  ② a same-key grant→revoke race resolves by LOG ORDER (append order),
     never by ``ts`` (display-only) — see ``approval_ledger.py``'s own
     "Ordering authority" section
  ③ folding the ledger after migrating a legacy ``approvals.yaml``
     snapshot reproduces exactly what the OLD snapshot reader returned
  ④ (documented, not tested — a stated caveat, not a behavior): append
     atomicity depends on each record line staying under the OS's own
     atomic-write threshold

①/② are witnessed with REAL, SEPARATE OS processes (``multiprocessing``)
writing to the SAME ledger file at the same time — lead-coder's own
explicit instruction: "逐次に書いて通しても意味がありません" (sequential
writes in one process would prove nothing about concurrent-writer safety;
BudgetLedger has no such precedent test either, so this is the FIRST rigor
check of this shape in this codebase for an append-only ledger).
"""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from reyn.security.permissions.approval_ledger import (
    ApprovalLedger,
    migrate_legacy_snapshot,
)


def _worker_append_many(path_str: str, key: str, n: int) -> None:
    """Module-level (picklable) worker: append *n* alternating
    grant/revoke records for *key* — run in a SEPARATE OS process."""
    ledger = ApprovalLedger(Path(path_str))
    for i in range(n):
        ledger.append_approval(key, approved=(i % 2 == 0))


def _worker_append_one(path_str: str, key: str, approved: bool) -> None:
    ApprovalLedger(Path(path_str)).append_approval(key, approved)


def test_two_real_processes_approving_different_keys_both_survive(tmp_path: Path) -> None:
    """Tier 2: #5153 acceptance ① — two SEPARATE OS processes, each
    appending its OWN key many times concurrently, must both fully land
    (no lost lines, no corrupted lines) -- the exact shape the OLD
    snapshot read-modify-write lost silently under this scenario."""
    ledger_path = tmp_path / "approvals.jsonl"
    n = 200

    p1 = multiprocessing.Process(
        target=_worker_append_many, args=(str(ledger_path), "actor/file.write/dir_a/", n),
    )
    p2 = multiprocessing.Process(
        target=_worker_append_many, args=(str(ledger_path), "actor/file.write/dir_b/", n),
    )
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    ledger = ApprovalLedger(ledger_path)
    lines = list(ledger.path.read_text(encoding="utf-8").splitlines())
    expected_total = 2 * n
    written_total = len(lines)
    assert written_total == expected_total, (
        f"expected {expected_total} lines (no lost/dropped appends under "
        f"real concurrent writers), got {written_total}"
    )
    for line in lines:
        json.loads(line)  # every line parses -- no interleaved corruption

    approvals, _bound = ledger.fold()
    # n is even, so the LAST record each process wrote (index n-1, odd) is
    # approved=False for both keys.
    assert approvals["actor/file.write/dir_a/"] is False
    assert approvals["actor/file.write/dir_b/"] is False


def test_two_real_processes_racing_the_same_key_resolve_by_log_order(
    tmp_path: Path,
) -> None:
    """Tier 2: #5153 acceptance ② — two SEPARATE OS processes racing a
    grant vs. a revoke for the SAME key: whichever one the OS actually
    wrote LAST (file order) must be what ``fold()`` returns -- verified by
    reading the ledger's own last line back, not by assuming a winner
    (the whole point: the winner is legitimately nondeterministic under
    real concurrency, and the ledger's job is to be INTERNALLY consistent
    with whatever that real order was, not to predict it)."""
    ledger_path = tmp_path / "approvals.jsonl"
    key = "actor/file.write/contested/"

    p1 = multiprocessing.Process(
        target=_worker_append_one, args=(str(ledger_path), key, True),
    )
    p2 = multiprocessing.Process(
        target=_worker_append_one, args=(str(ledger_path), key, False),
    )
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    ledger = ApprovalLedger(ledger_path)
    lines = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    written_total = len(lines)
    assert written_total == 2, "both processes' appends must survive -- neither lost"
    last_record_approved = lines[-1]["approved"]

    approvals, _bound = ledger.fold()
    assert approvals[key] == last_record_approved, (
        "fold() must agree with whatever the ledger's OWN last line says "
        "for this key -- last wins by FILE ORDER, not by which process "
        "happened to win the OS scheduler"
    )


def test_migration_witness_fold_matches_the_legacy_reader(tmp_path: Path) -> None:
    """Tier 2: #5153 acceptance ③ — migrating a legacy approvals.yaml
    snapshot into the ledger, then folding it, must reproduce EXACTLY
    what the OLD (pre-#5153) snapshot reader returned -- migration adds
    records, it does not reinterpret them."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    legacy_path = reyn_dir / "approvals.yaml"
    legacy_path.write_text(
        "actor/file.write/legacy_dir/: true\n"
        "actor/file.write/legacy_revoked/: false\n"
        "actor/http.get/example.com: true\n"
        "_bound_identities:\n"
        "  actor/file.write/legacy_dir/:\n"
        "    ino: 12345\n"
        "    birthtime: 1700000000.0\n",
        encoding="utf-8",
    )

    # The OLD reader's own logic (mirrors _load_saved/_load_bound_identities
    # exactly as they read before #5153 -- this is the "expected" side of
    # the witness, not a re-derivation of the new code under test).
    import yaml
    raw = yaml.safe_load(legacy_path.read_text(encoding="utf-8")) or {}
    expected_saved = {k: bool(v) for k, v in raw.items() if isinstance(v, bool)}
    expected_bound_raw = raw.get("_bound_identities") or {}
    expected_bound = {
        k: (v["ino"], v.get("birthtime"))
        for k, v in expected_bound_raw.items()
        if isinstance(v, dict) and "ino" in v
    }

    ledger = ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl")
    migrate_legacy_snapshot(ledger, legacy_path)
    saved, bound = ledger.fold()

    assert saved == expected_saved
    assert bound == expected_bound


def test_migration_is_idempotent_a_second_call_is_a_no_op(tmp_path: Path) -> None:
    """Tier 2: #5153 — migration must not re-run once the ledger exists,
    so every caller can call it unconditionally before every read/append
    without needing its own "have I migrated" flag."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    legacy_path = reyn_dir / "approvals.yaml"
    legacy_path.write_text("actor/file.write/dir/: true\n", encoding="utf-8")

    ledger = ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl")
    migrate_legacy_snapshot(ledger, legacy_path)
    lines_after_first = ledger.path.read_text(encoding="utf-8").splitlines()

    migrate_legacy_snapshot(ledger, legacy_path)
    lines_after_second = ledger.path.read_text(encoding="utf-8").splitlines()

    assert lines_after_first == lines_after_second, (
        "a second migrate_legacy_snapshot call must be a no-op -- the "
        "ledger already existing is what makes it skip"
    )
