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

CI red (lead-coder, PR #5170, Python 3.12 only): one such test got 401
lines instead of 400 -- an EXTRA line, not a lost one. The job's own
warnings carried the actual cause: "This process (pid=...) is
multi-threaded, use of fork() may lead to deadlocks in the child" --
plain `multiprocessing.Process(...)` (this test's ORIGINAL form) with no
explicit context uses the PLATFORM DEFAULT start method, which is
"fork" on Linux (CPython's own
docs: unsafe when the parent has more than one thread, exactly what a
pytest-xdist worker is) but "spawn" on macOS (this dev machine, since
Python 3.8) -- fork()ing a multi-threaded parent can leave the child
with another thread's lock (buffering, import, etc.) held forever, or
in another inconsistent state, with no thread present to release/fix
it. This is why the failure never reproduced locally: it needs the
Linux+multi-threaded-parent combination CI has and this machine does
not. Fixed by using an EXPLICIT ``multiprocessing.get_context("spawn")``
for every ``Process`` in this file -- ``spawn`` re-imports fresh in the
child regardless of platform default, matching what already happened
to work by accident on macOS, so the same safe behavior is now
guaranteed everywhere this test runs, not just here.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

from reyn.security.permissions.approval_ledger import (
    ApprovalLedger,
    migrate_legacy_snapshot,
)

# See the module docstring's "CI red" note: explicit spawn, never the
# platform default, so this test's own correctness never depends on
# whether the pytest worker that runs it happens to be single-threaded.
_mp = multiprocessing.get_context("spawn")


def _worker_append_many(path_str: str, key: str, n: int) -> None:
    """Module-level (picklable) worker: append *n* alternating
    grant/revoke records for *key* — run in a SEPARATE OS process."""
    ledger = ApprovalLedger(Path(path_str))
    for i in range(n):
        ledger.append_approval(key, approved=(i % 2 == 0))


def _worker_append_one(path_str: str, key: str, approved: bool) -> None:
    ApprovalLedger(Path(path_str)).append_approval(key, approved)


def _worker_migrate(ledger_path_str: str, legacy_path_str: str) -> None:
    ledger = ApprovalLedger(Path(ledger_path_str))
    migrate_legacy_snapshot(ledger, Path(legacy_path_str))


def _worker_revoke(ledger_path_str: str, legacy_path_str: str, key: str) -> None:
    """Mirrors the REAL call pattern every caller in this codebase
    follows (``PermissionResolver._ensure_folded``, the CLI/web
    ``_load``): migrate first (win or lose), THEN append the real
    decision -- never a bare append with no migrate attempt at all,
    which no real call site does."""
    ledger = ApprovalLedger(Path(ledger_path_str))
    migrate_legacy_snapshot(ledger, Path(legacy_path_str))
    ledger.append_approval(key, False)


def _diagnose_line_count_mismatch(raw_content: str, expected_total: int) -> str:
    """#5192: build a SELF-DESCRIBING failure message for a line-count
    mismatch instead of a bare ``got N``.

    Architect ruling (issuecomment-5384594944, #5192): "increased" collapses
    AT LEAST 4 structurally different causes into the same ``assert 401 ==
    400`` — (1) a write split into two lines (the PIPE_BUF atomicity caveat
    ``approval_ledger.py``'s own docstring names), (2) an embedded blank
    line (a ``splitlines()`` vs a raw ``split("\\n")`` counting difference),
    (3) a record of a DIFFERENT ``kind`` leaking in (``identity_bind``/a
    migration record), (4) an exact-duplicate record. All four print the
    identical bare number, so the NEXT failure must name which one — this
    is the diagnostic, not a fix for any of the four.

    Returns a multi-paragraph message naming: the per-``kind`` breakdown,
    every line that failed to parse as JSON (raw bytes, not just a count —
    witness for cause 1), how many embedded blank lines exist (witness for
    cause 2), and any exact-duplicate line (witness for cause 4). Cause 3
    falls out of the kind breakdown directly."""
    raw_lines = raw_content.split("\n")
    # A trailing "\n" (every successful _write_record ends with one) makes
    # split("\n") emit one final "" that is NOT an embedded blank line —
    # drop exactly that one, if present, before counting embedded blanks.
    if raw_lines and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]
    blank_line_count = sum(1 for line in raw_lines if line == "")
    non_blank_lines = [line for line in raw_lines if line != ""]

    kind_counts: "dict[str, int]" = {}
    unparseable: "list[str]" = []
    seen: "dict[str, int]" = {}
    duplicates: "list[str]" = []
    for line in non_blank_lines:
        seen[line] = seen.get(line, 0) + 1
        if seen[line] == 2:  # report each duplicated line once, at its 2nd sighting
            duplicates.append(line)
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            unparseable.append(line)
            continue
        kind = rec.get("kind") if isinstance(rec, dict) else None
        kind_counts[str(kind)] = kind_counts.get(str(kind), 0) + 1

    return (
        f"expected {expected_total} lines (no lost/dropped appends under real "
        f"concurrent writers), got {len(non_blank_lines)} non-blank + "
        f"{blank_line_count} blank = {len(raw_lines)} raw lines.\n"
        f"  kind breakdown: {kind_counts!r}\n"
        f"  unparseable lines ({len(unparseable)}): {unparseable!r}\n"
        f"  embedded blank lines: {blank_line_count}\n"
        f"  exact-duplicate lines ({len(duplicates)}): {duplicates!r}"
    )


def test_two_real_processes_approving_different_keys_both_survive(tmp_path: Path) -> None:
    """Tier 2: #5153 acceptance ① — two SEPARATE OS processes, each
    appending its OWN key many times concurrently, must both fully land
    (no lost lines, no corrupted lines) -- the exact shape the OLD
    snapshot read-modify-write lost silently under this scenario."""
    ledger_path = tmp_path / "approvals.jsonl"
    n = 200

    p1 = _mp.Process(
        target=_worker_append_many, args=(str(ledger_path), "actor/file.write/dir_a/", n),
    )
    p2 = _mp.Process(
        target=_worker_append_many, args=(str(ledger_path), "actor/file.write/dir_b/", n),
    )
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    ledger = ApprovalLedger(ledger_path)
    raw_content = ledger.path.read_text(encoding="utf-8")
    lines = raw_content.splitlines()
    expected_total = 2 * n
    written_total = len(lines)
    assert written_total == expected_total, _diagnose_line_count_mismatch(
        raw_content, expected_total,
    )
    for line in lines:
        json.loads(line)  # every line parses -- no interleaved corruption

    # #5192 (architect ruling, issuecomment-5384594944): a RAW LINE count
    # is the wrong population to assert "no increase" over -- an embedded
    # blank line inflates it for a reason unrelated to record integrity.
    # The record count iter_records()/fold() actually SEE is the real
    # acceptance: exactly N records, kind breakdown included, so a future
    # regression that leaks a stray identity_bind/migration record (or
    # drops one) is caught by CONTENT, not by counting newlines.
    records = list(ledger.iter_records())
    kind_counts: "dict[str, int]" = {}
    for rec in records:
        kind_counts[str(rec.get("kind"))] = kind_counts.get(str(rec.get("kind")), 0) + 1
    assert len(records) == expected_total, (
        f"iter_records() saw {len(records)} records, expected {expected_total} "
        f"(kind breakdown: {kind_counts!r})"
    )
    assert kind_counts == {"approval": expected_total}, (
        f"unexpected kind breakdown: {kind_counts!r} -- every record here "
        f"should be an 'approval' from _worker_append_many, nothing else"
    )

    approvals, _bound, _scopes = ledger.fold()
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

    p1 = _mp.Process(
        target=_worker_append_one, args=(str(ledger_path), key, True),
    )
    p2 = _mp.Process(
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

    approvals, _bound, _scopes = ledger.fold()
    assert approvals[key] == last_record_approved, (
        "fold() must agree with whatever the ledger's OWN last line says "
        "for this key -- last wins by FILE ORDER, not by which process "
        "happened to win the OS scheduler"
    )


def test_no_lead_newline_check_remains_in_the_module() -> None:
    """Tier 2: #5192 acceptance ① (architect ruling, issuecomment-
    5384627324) — the removed check-then-write guard's own identifier is
    TOTALLY ABSENT from approval_ledger.py, not merely unreachable. "Never
    called" would still leave the method defined (dead code drifting back
    into use later); this asserts the stronger claim by reading the actual
    module source, the same population a real ``grep`` would search."""
    import inspect

    import reyn.security.permissions.approval_ledger as approval_ledger_module

    source = inspect.getsource(approval_ledger_module)
    assert "_needs_lead_newline" not in source, (
        "the old TOCTOU-vulnerable guard's identifier must be completely "
        "removed, not merely unused -- see _write_record's own docstring"
    )


def test_a_ledger_missing_its_trailing_newline_is_repaired_by_migration(
    tmp_path: Path,
) -> None:
    """Tier 2: #5192 acceptance ③ — a pre-existing ledger file that does
    NOT end in a trailing newline (simulating a torn write from before
    this fix, or any other historical cause) is repaired the ONE TIME
    migrate_legacy_snapshot runs against it -- not on every append (see
    _repair_missing_trailing_newline's own docstring for why that's a
    migration-time, not a per-append, concern)."""
    ledger_path = tmp_path / "approvals.jsonl"
    # Simulate a torn write: two well-formed records, but the FILE's own
    # last byte is not "\n" (the second record's own trailing newline is
    # missing -- exactly what a crash mid-fsync could leave behind).
    torn_content = (
        json.dumps({"kind": "approval", "key": "a", "approved": True}) + "\n"
        + json.dumps({"kind": "approval", "key": "b", "approved": False})
    )
    ledger_path.write_text(torn_content, encoding="utf-8")
    assert not torn_content.endswith("\n"), "sanity: the fixture is genuinely torn"

    ledger = ApprovalLedger(ledger_path)
    legacy_yaml_path = tmp_path / "approvals.yaml"  # never created -- not this test's path
    migrate_legacy_snapshot(ledger, legacy_yaml_path)

    assert ledger.path.read_text(encoding="utf-8").endswith("\n"), (
        "migrate_legacy_snapshot must repair a missing trailing newline on "
        "an already-existing ledger file"
    )

    # The repair must not corrupt or duplicate what was already there --
    # a fresh append now lands as its OWN clean line, not concatenated
    # onto key "b"'s own (now newline-terminated) record.
    ledger.append_approval("c", True)
    records = list(ledger.iter_records())
    assert [(r["key"], r["approved"]) for r in records] == [
        ("a", True), ("b", False), ("c", True),
    ], f"unexpected records after repair + append: {records!r}"


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
    saved, bound, _scopes = ledger.fold()
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


def test_two_processes_racing_the_first_migration_still_fold_correctly(
    tmp_path: Path,
) -> None:
    """Tier 2: #5153 — architect's TESTS-READ(A) note for the B reviewer
    (issuecomment-5384009424): ``_load`` calls ``migrate_legacy_snapshot``
    unconditionally on every touch, so two processes could both see "no
    ledger yet" and both migrate at once. Measured (not assumed, per
    architect's own "私は測っていません"): with REAL separate OS
    processes racing the FIRST migration of the SAME legacy snapshot,
    each migration only ever APPENDS (never truncates/replaces), so a
    doubled set of day-0 records is, at worst, redundant -- ``fold()``'s
    own last-wins-per-key semantics collapse duplicate identical records
    to the same value, same as a single migration would have produced."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    legacy_path = reyn_dir / "approvals.yaml"
    legacy_path.write_text(
        "actor/file.write/legacy_dir/: true\n"
        "actor/file.write/legacy_revoked/: false\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / ".reyn" / "approvals.jsonl"

    p1 = _mp.Process(
        target=_worker_migrate, args=(str(ledger_path), str(legacy_path)),
    )
    p2 = _mp.Process(
        target=_worker_migrate, args=(str(ledger_path), str(legacy_path)),
    )
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    saved, _bound, _scopes = ApprovalLedger(ledger_path).fold()
    assert saved == {
        "actor/file.write/legacy_dir/": True,
        "actor/file.write/legacy_revoked/": False,
    }


def test_a_slow_migration_racing_a_real_revoke_never_resurrects_the_stale_value(
    tmp_path: Path,
) -> None:
    """Tier 2: #5153 — docs-maintainer's TESTS-READY(B) finding on PR
    #5170 (issuecomment-5384027134, 3/3, no strip needed): a slow
    migration (many legacy keys, target key not first in iteration order)
    racing a genuinely CONCURRENT real revoke of that SAME key must NOT
    let the migration's stale legacy value land in file order AFTER the
    real revoke -- that would make ``fold()`` resurrect an already-revoked
    approval, straight at the permission band's own correctness.

    Reproduced with a large legacy snapshot (many keys before the target,
    so a per-key-append migration takes measurably longer than one fast
    real append) and 2 REAL separate OS processes -- no artificial delay,
    no strip needed to see this fail on the pre-fix (one-append-per-key)
    migration; this test is the reproduction AND the fix's own witness."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    legacy_path = reyn_dir / "approvals.yaml"
    target_key = "actor/file.write/target_dir/"

    other_rows = "\n".join(
        f"actor/file.write/other_{i}/: true" for i in range(3000)
    )
    legacy_path.write_text(
        other_rows + f"\n{target_key}: true\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / ".reyn" / "approvals.jsonl"

    p_migrate = _mp.Process(
        target=_worker_migrate, args=(str(ledger_path), str(legacy_path)),
    )
    p_revoke = _mp.Process(
        target=_worker_revoke, args=(str(ledger_path), str(legacy_path), target_key),
    )
    p_migrate.start()
    p_revoke.start()
    p_migrate.join(timeout=60)
    p_revoke.join(timeout=60)
    assert p_migrate.exitcode == 0
    assert p_revoke.exitcode == 0

    saved, _bound, _scopes = ApprovalLedger(ledger_path).fold()
    assert saved[target_key] is False, (
        "a real, concurrent revoke must never be resurrected by a slow "
        "migration's stale legacy value landing after it in file order"
    )


def test_migration_publish_never_clobbers_a_decision_that_wins_first(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #5153 — a DETERMINISTIC proof of the exact invariant the
    real-process test above only demonstrates probabilistically (OS
    scheduling/spawn overhead makes forcing an exact interleave
    unreliable to control from the outside). Forces a real decision to
    land in the NARROWEST possible window — the instant before
    ``migrate_legacy_snapshot``'s own ``os.link`` publish call, after its
    temp file is already fully written — by monkeypatching ``os.link``
    itself (the actual publish primitive, not a fake collaborator: the
    ledger, the temp file, and the real decision are all real) to run one
    real ``append_approval`` immediately before delegating to the real
    ``os.link``. This is the tightest possible race this module can ever
    face — if migration survives THIS without clobbering the real
    decision, it survives any looser (real-world) timing too."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    legacy_path = reyn_dir / "approvals.yaml"
    target_key = "actor/file.write/target_dir/"
    legacy_path.write_text(f"{target_key}: true\n", encoding="utf-8")
    ledger_path = tmp_path / ".reyn" / "approvals.jsonl"
    ledger = ApprovalLedger(ledger_path)

    real_link = os.link

    def _real_decision_wins_the_narrowest_window(src, dst):
        # The real decision creates the ledger path for the FIRST time,
        # right before migration's own link() call would have.
        ApprovalLedger(ledger_path).append_approval(target_key, False)
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", _real_decision_wins_the_narrowest_window)
    migrate_legacy_snapshot(ledger, legacy_path)

    saved, _bound, _scopes = ledger.fold()
    assert saved[target_key] is False, (
        "migration must never clobber a real decision, even one that "
        "wins the ledger path's existence in the narrowest possible "
        "window right before migration's own publish step"
    )
