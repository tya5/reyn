"""#5153: append-only JSONL ledger for permission-approval decisions.

Root cause (lead-coder): ``.reyn/approvals.yaml``'s persistence shape was
snapshot read-modify-write — ``_persist`` (and, after #5152,
``_bind_identity``) does ``yaml.safe_load`` the WHOLE file → mutate a dict →
``write_text`` the WHOLE file back. Every writer therefore needs momentary
ownership of the ENTIRE file's content to make even a ONE-key change, and
TWO writers doing this concurrently (the owner's actual configuration: a
``reyn web`` server plus one or more ``--connect`` CLI clients, all sharing
the same project) silently lose an approval — last-writer-wins, with no
audit trail of the lost decision ever having existed.

Architect ruling (issuecomment-5383838646): don't decide who owns the file
— make owning it unnecessary. Shrink what a writer must produce down to
"my own decision" (one record), append-only, mirroring
:class:`reyn.runtime.budget.budget.BudgetLedger` (PR25) exactly — the same
codebase precedent for "durable, fsync'd-on-append, folded by readers"
already used for LLM-call cost records, restated in the constitution as
"the ledger, not the best-effort state-file cache, wins on recovery".
Concurrent appends to DIFFERENT keys never conflict (they're different
lines); concurrent appends to the SAME key both survive, in whatever order
the OS interleaves the two small writes, and folding resolves that
ordering deterministically (last record per key wins) — no reader ever
needs to arbitrate between them, because neither write ever had to see or
replace the other's line.

**What goes in this ledger** (architect ruling): approval decisions
(``kind="approval"``) AND #5152's own bound-identity records
(``kind="identity_bind"``) — the SAME log, not two. Bound-identity records
have become the MOST frequent write since #5152 (bind-on-first-use fires
on every path approval's first use per process, not just on a human
decision), so folding them separately would just move the race rather than
close it.

**Fold semantics** (the read side): walk records in file order; for each
``key``, the LAST ``"approval"`` record wins for whether it's granted, and
the LAST ``"identity_bind"`` record wins for the bound identity — EXCEPT a
``"approval"`` record with ``approved=False`` (a revoke) also clears any
bound identity for that key, mirroring #5157's own revoke-clears-binding
invariant (a stale identity surviving a revoke is the exact "a name is not
an identity" shape #5042 exists to close). This requires no locking and no
read-before-write: a fold is a pure function of "every record written so
far", and two processes racing to append never need to agree on anything
before their own write lands.

**Migration** (acceptance ③): a pre-#5153 ``approvals.yaml`` snapshot
(``{key: bool}`` rows plus #5152's own ``_bound_identities`` sibling
section) is migrated ONCE, on first touch, into day-0 records — see
:func:`migrate_legacy_snapshot`. Folding the ledger after migration must
reproduce EXACTLY the same effective state the old snapshot reader would
have returned (the acceptance witness) — migration adds records, it does
not reinterpret them. The migration itself is written to a temp file,
fully durable, THEN published at the ledger's own path via an atomic
``os.link`` (not one append call per legacy key, and not a plain
exclusive-create-then-write either — see that function's own docstring
for why both of those still leave a gap) — see that function's own
docstring for the real defect (docs-maintainer's TESTS-READY(B), PR
#5170) this closes: a partially-migrated ledger racing
a genuine concurrent decision could let a STALE legacy value land after,
and therefore override, a real revoke.

**Ordering authority** (architect co-vet, broker, #5153 2026-08-23T02:44Z):
FILE ORDER is the ONLY authority "last wins" means — i.e. the order
:meth:`iter_records` yields lines in, which is append order, which is
whatever order the OS actually interleaved N concurrent processes' small
``write()`` calls in.
The ``ts`` field on every record is DISPLAY-ONLY — never sorted on, never
compared, never used to break or resolve anything. Independent processes'
clocks are not synchronized (skew, NTP drift, two records landing in the
same wall-clock second under real concurrency), so a fold that "helpfully"
re-orders by ``ts`` would silently reintroduce exactly the same ambiguity
this ledger exists to remove — the same "one fact, two sources of truth"
shape this codebase closed 3 times over the same night this issue was
ruled on. :meth:`fold` must never read ``ts`` for anything but a caller's
own display formatting.

**Scope (#5052)**: every approval decision now carries a ``scope`` value
naming WHO it applies to — ``SCOPE_WORKSPACE`` ("every agent in this
workspace") or an ``agent:<name>`` string built by :func:`scope_for_agent`
("this one agent only"). This is a VALUE on the record, never a position in
``key`` (architect ruling, issuecomment-5384686461, "C" — the repo's own
standing "typed discriminated union over form-sniffed string" preference):
putting the agent into the key string, or splitting into per-agent files,
was explicitly rejected — the former can't tell "no agent dimension" apart
from "matches everything", and the latter breaks the #5065 single-audit-
surface goal. A THIRD value, ``session:<sid>``, was also explicitly
rejected (architect + lead-coder, broker 2026-08-24): ``sid`` is generated
by ``uuid4().hex[:8]`` (32 bits) and the registry keeps zero record of a
retired session id (measured: ``registry.py``'s ``_has_session`` only
checks CURRENTLY-LIVE sessions), so a session id CAN be reused — a
session-scoped approval would silently reattach to an unrelated later
session using the same id, which is worse than not having the scope at
all.

Append-only means a pre-#5052 record — written before this field existed —
can NEVER be rewritten to carry one (rewriting a past ledger line is
ledger falsification, the constitution's "does the repair destroy the
evidence" question, answered "yes"). :meth:`fold` therefore treats a
record with no ``scope`` key as ``SCOPE_LEGACY_WORKSPACE`` — deliberately
a DIFFERENT sentinel from ``SCOPE_WORKSPACE``, never collapsed into it
(the #4996-family discipline: "unspecified" and "specified-and-wide" are
not the same value) — but one that matches the CURRENT-agent check the
same way ``SCOPE_WORKSPACE`` does, because a pre-#5052 grant genuinely
WAS a workspace-wide decision the moment it was made (there was no agent
dimension for the operator to have narrowed). A fresh write never
produces ``SCOPE_LEGACY_WORKSPACE`` — it is a read-time-only classification
of absence, synthesized by :meth:`fold`, never persisted as a literal
string. The moment a legacy key is re-approved, the newest record (which
DOES carry an explicit scope) wins the fold, same as any other key.

**Caveat, stated rather than solved** (acceptance ④): the append's
atomicity depends on each record line being small — a local filesystem's
``write()`` for a buffer at or under ``PIPE_BUF``/the OS's own atomic-write
threshold does not interleave with a concurrent writer's own ``write()``.
This is the same assumption ``BudgetLedger`` already makes (never
challenged there) and is NOT re-derived or strengthened here; a record
line long enough to exceed that threshold (implausible for this ledger's
small dict shapes) would need a different guarantee this module does not
provide.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

#: Canonical project-relative path to the ledger file — the ONE name every
#: other module builds a Path from: ``PermissionResolver.__init__``, and (#5173)
#: the #1199 write-gate carve-out's ``_CANONICAL_PROTECTED_WRITE_PATHS`` in
#: BOTH its copies (``security/permissions/permissions.py`` and
#: ``api/safe/file.py`` — the latter imports this constant directly rather than
#: re-typing it; it cannot import the rest of this security package's parent
#: module, but this module has zero reyn-internal imports, so it is safe to).
#:
#: This constant is the fix for the actual root cause #5173 found: when
#: persistence moved off ``approvals.yaml`` (#5153/#5170), the write-gate
#: carve-out — a hand-typed literal at each of 2 use sites — silently did not
#: follow, reopening #1199's own approval-injection bypass against the new
#: live file. A literal can drift from what it names; a single constant that
#: every dependent site imports cannot — renaming the ledger means changing
#: this ONE line, and every dependent site (the resolver's own path AND the
#: write-gate that protects it) moves together by construction.
RELATIVE_PATH = ".reyn/approvals.jsonl"

#: #5052: an approval that applies to every agent in the workspace. Only
#: ever written when an operator EXPLICITLY chose the wide grant — never
#: the silent default (see :func:`scope_for_agent`'s own docstring for the
#: default reasoning).
SCOPE_WORKSPACE = "workspace"

#: #5052: read-time-only classification for a record that predates the
#: ``scope`` field entirely (folded from a record with no ``"scope"`` key —
#: see the module docstring's "Scope" section). Deliberately a DIFFERENT
#: string from ``SCOPE_WORKSPACE`` — "this record never said" and "this
#: record said workspace" must never collapse into the same value, even
#: though both currently match every agent at lookup time. NEVER passed to
#: :meth:`ApprovalLedger.append_approval` — a fresh write always carries an
#: explicit scope, so this string can never appear as a literal on disk,
#: only as :meth:`ApprovalLedger.fold`'s own in-memory classification.
SCOPE_LEGACY_WORKSPACE = "legacy-workspace"


def scope_for_agent(agent_name: str) -> str:
    """#5052: the ``agent:<name>`` scope value naming ONE agent.

    Default-scope reasoning (lead-coder ruling, issuecomment-5386
    -family — reproduced here verbatim since acceptance ① requires the
    default's justification live as a code comment at its definition
    site, not only in the issue thread):

    1. This is a "which direction is safer" question, not one where the
       owner holds information the implementer lacks — the answer
       follows from the risk shape alone, so it does not need to be
       deferred.
    2. The risk is ASYMMETRIC. Defaulting NARROW (``agent:<name>``) and
       being wrong just means an operator is asked again for a second
       agent — one extra prompt. Defaulting WIDE (``workspace``) and
       being wrong means one agent's approval silently governs EVERY
       agent in the workspace — the exact #5052 root cause, hitting the
       permission band directly.
    3. It is reversible in only ONE direction: an operator can WIDEN a
       narrow grant later (re-approve with the workspace choice, or a
       config override). There is no way to retroactively NARROW a
       workspace grant that has already silently covered agents the
       operator never saw prompted for.

    ∴ the default for a genuine per-running-agent decision is this
    function's own output, not :data:`SCOPE_WORKSPACE`."""
    return f"agent:{agent_name}"


class ApprovalLedger:
    """Append-only JSONL ledger for one project's permission-approval
    decisions and bound-identity records — see the module docstring for
    the full rationale. One instance per ``approvals.jsonl`` path; safe to
    construct freely (no held state beyond the path), same as
    ``BudgetLedger`` — a CLI subcommand or a web request handler with no
    live ``PermissionResolver`` instance can use one exactly the same way
    a running session does.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append_approval(
        self, key: str, approved: bool, scope: "str | None" = None,
    ) -> None:
        """Append one approval-decision record (a grant or a revoke).
        ``ts`` is DISPLAY-ONLY (see the module docstring's "Ordering
        authority" section) — :meth:`fold` never reads it.

        #5052: ``scope`` is :data:`SCOPE_WORKSPACE` or a
        :func:`scope_for_agent` string — every REAL caller in this
        codebase passes one explicitly (never relies on the ``None``
        default here); ``None`` exists only so a record shaped like a
        pre-#5052 legacy write can be constructed on purpose (tests
        exercising the legacy-migration read path). A record written
        with ``scope=None`` omits the ``"scope"`` key entirely rather
        than writing a literal ``null`` — ``fold()`` distinguishes
        "key absent" from "key present with a falsy value" the same
        way, but omitting it keeps a legacy-shaped test record
        byte-for-byte indistinguishable from a genuine pre-#5052 line."""
        record: "dict[str, Any]" = {
            "ts": self._now_iso(),
            "kind": "approval",
            "key": key,
            "approved": approved,
        }
        if scope is not None:
            record["scope"] = scope
        self._write_record(record)

    def append_identity_bind(
        self, key: str, ino: int, birthtime: "float | None",
    ) -> None:
        """Append one #5152 bound-identity record for *key*."""
        self._write_record({
            "ts": self._now_iso(),
            "kind": "identity_bind",
            "key": key,
            "ino": ino,
            "birthtime": birthtime,
        })

    @staticmethod
    def _now_iso() -> str:
        """Current local time as an ISO-8601 string with UTC offset — same
        shape as ``BudgetLedger._now_iso``."""
        lt = time.localtime(time.time())
        offset_sec = lt.tm_gmtoff
        sign = "+" if offset_sec >= 0 else "-"
        offset_abs = abs(offset_sec)
        offset_str = f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
        return (
            f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
            f"{offset_str}"
        )

    def _write_record(self, record: dict) -> None:
        """Serialize *record* as one SELF-TERMINATING JSONL line (always
        ``line + "\\n"``, unconditionally) and append it — no read of any
        kind happens on this path.

        #5192 (architect ruling, issuecomment-5384627324, self-diagnosed
        design flaw): this method used to mirror the sibling ``BudgetLedger``
        class's own "leading-newline guard" — a pre-check (``stat()`` + a
        separate ``open("rb")``/``seek``/``read`` of the file's own tail
        byte) that inserted a leading ``"\\n"`` before the record whenever
        the file's LAST byte wasn't already ``"\\n"``, defending against a
        torn (no-trailing-newline) write left by a crash. Deliberately not
        naming that check's old identifier here — acceptance ① for this fix
        is that identifier's TOTAL absence from this module (a grep hit in
        a docstring would still count). docs-maintainer's
        live 3-stage repro (issuecomment-5384615994) confirmed this check
        is ITSELF a TOCTOU race: under real concurrent writers, it can
        observe another process's write mid-flight — its trailing ``"\\n"``
        landed in the kernel but not yet visible to this read, or vice
        versa — and insert a spurious lead newline, producing exactly the
        "400 appends -> 401 lines" symptom (a bystander blank line; ``fold()``
        already tolerated it silently via its own ``if not line: continue``,
        so approvals were never corrupted — only the RAW LINE COUNT was ever
        wrong).

        The fix is not a smarter/locked/retried guard (all three still read
        before writing, the exact thing this ledger's whole design exists to
        make unnecessary — "shrink what a writer must produce down to my own
        decision" is the architect ruling that created this class, #5153's
        own module docstring) — it is removing the read entirely. A torn
        write from BEFORE this fix (or any file this ledger did not itself
        create) is a MIGRATION-time concern, handled ONCE by
        :func:`migrate_legacy_snapshot` (see its own docstring), not
        something every append needs to re-verify. Every record this method
        writes is, by construction, exactly one line ending in ``"\\n"`` —
        concatenation onto a prior malformed tail can only happen if the
        FILE was already broken before this ledger's own append-only
        writing began, which migration is what repairs.
        """
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def iter_records(self):
        """Yield parsed record dicts; skip broken/non-dict lines (a torn
        write from a crash mid-append reads as absent, never an error —
        the SAME tolerance ``BudgetLedger.iter_records`` gives its own
        lines)."""
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                yield entry

    def fold(
        self,
    ) -> "tuple[dict[str, bool], dict[str, tuple[int, float | None]], dict[str, str]]":
        """Replay every record in file order into the three maps
        ``PermissionResolver`` needs: ``(approvals, bound_identities, scopes)``.

        Last ``"approval"`` record per key wins for the boolean AND for the
        scope (they come from the SAME record — a re-approval always
        replaces both together, never one without the other); last
        ``"identity_bind"`` record per key wins for the identity — EXCEPT
        an ``"approval"`` record with ``approved=False`` also clears that
        key's bound identity (see the module docstring's "Fold semantics"
        section for why this mirrors #5157, not a new rule).

        #5052: ``scopes[key]`` is the record's own ``"scope"`` value when
        present, else :data:`SCOPE_LEGACY_WORKSPACE` — see the module
        docstring's "Scope" section for why a missing field is a
        DIFFERENT value from an explicit ``SCOPE_WORKSPACE``, never
        collapsed into it, even though both currently match every agent
        at lookup time."""
        approvals: "dict[str, bool]" = {}
        bound: "dict[str, tuple[int, float | None]]" = {}
        scopes: "dict[str, str]" = {}
        for rec in self.iter_records():
            key = rec.get("key")
            if not isinstance(key, str):
                continue
            kind = rec.get("kind")
            if kind == "approval":
                approved = rec.get("approved")
                if not isinstance(approved, bool):
                    continue
                approvals[key] = approved
                scope = rec.get("scope")
                scopes[key] = scope if isinstance(scope, str) and scope else SCOPE_LEGACY_WORKSPACE
                if not approved:
                    bound.pop(key, None)
            elif kind == "identity_bind":
                ino = rec.get("ino")
                if not isinstance(ino, int):
                    continue
                bt = rec.get("birthtime")
                bound[key] = (ino, float(bt) if isinstance(bt, (int, float)) else None)
        return approvals, bound, scopes


def _repair_missing_trailing_newline(path: Path) -> None:
    """#5192: ONE-TIME repair for a pre-existing ledger file that does not
    end in a trailing newline — a torn write from before this fix landed,
    or any other historical cause. Never something a healthy, self-
    terminating :meth:`ApprovalLedger._write_record` can produce going
    forward (every successful append writes exactly one line ending in
    ``"\\n"``, unconditionally) — so this repair only exists to clean up
    a file this ledger did NOT create through its own current writes.

    Called from :func:`migrate_legacy_snapshot` — every real caller in
    this codebase invokes that before its own append (see that
    function's own docstring), so this repair rides the SAME call site
    rather than adding a new one. Deliberately NOT called from
    ``_write_record``'s own hot path: that per-append check-then-write
    was the TOCTOU race #5192 closed (docs-maintainer's live repro,
    issuecomment-5384615994) — moving the SAME shape of check here does
    not reintroduce that bug in practice, because it now runs once per
    migration attempt rather than once per append, but it is still a
    read-then-write and callers should not treat it as race-free.

    Best-effort: any read/write failure is swallowed. A still-broken
    tail after a failed repair attempt is no worse than before this
    function existed — the next append still lands as ``O_APPEND``'s own
    new line, concatenated onto whatever broken tail remains, exactly
    the pre-#5192 behavior for a file this repair could not fix. This
    function can only ever improve the situation, never worsen it."""
    try:
        if path.stat().st_size == 0:
            return
        with path.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) == b"\n":
                return
    except OSError:
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def migrate_legacy_snapshot(ledger: ApprovalLedger, legacy_yaml_path: Path) -> None:
    """#5153 acceptance ③: one-time migration of a pre-ledger
    ``approvals.yaml`` snapshot (``{key: bool}`` rows plus #5152's own
    ``_bound_identities`` sibling section) into day-0 ledger records.

    A no-op unless the ledger file does not exist yet AND the legacy
    snapshot does — idempotent by construction (a second call sees the
    ledger already present and does nothing), so callers can call this
    unconditionally before every read/append without needing their own
    "have I migrated yet" flag. Malformed/partial legacy content degrades
    the same way the pre-#5153 loaders already did (a non-bool row or a
    malformed ``_bound_identities`` entry is skipped, never an error).

    **Invariant (architect co-vet, broker, #5170 2026-08-23T03:53Z —
    state this in words so a future reader does not reintroduce the
    real defect below by going per-key): the legacy snapshot is a BASE,
    never an UPDATE.**
    If the ledger already holds ANY record at all — for this key or any
    other — migration adds NOTHING, full stop; it never selectively
    "fills in just the keys that aren't there yet." A per-key migration
    (checking each legacy key individually against what the ledger
    already has) is exactly the shape that reintroduces the race below —
    that granularity is what let a stale value land after a real
    decision for ONE key while the others were still fine. Keeping
    migration all-or-nothing at the FILE level, gated once on whether
    the ledger exists AT ALL, is what makes the fix below possible in
    the first place.

    #5153 real defect (docs-maintainer's TESTS-READY(B) on PR #5170,
    issuecomment-5384027134 — 3/3, no strip needed): the FIRST version of
    this function appended one record PER KEY via ``ApprovalLedger``'s
    normal ``append_*`` calls — durable per-call, but NOT atomic as a
    WHOLE. A slow migration (many legacy keys, the target key not first
    in iteration order) racing a genuinely CONCURRENT real decision (a
    different process revoking that SAME key) could interleave: the real
    revoke lands in file order BEFORE this function later reaches and
    appends the STALE legacy value for that key — "last wins by file
    order" then resurrects the stale, already-revoked grant. Batching
    this function's own writes into one atomic operation would only fix
    migration racing ITSELF (two processes both migrating); it does
    nothing for migration racing an unrelated real append, because the
    genuine danger is a partially-migrated ledger being visible to (and
    written into) from another process's real decision at all.

    Fixed by making migration INDIVISIBLE relative to ANY other writer,
    not just to another migration attempt: every legacy row (both
    ``approval`` and ``identity_bind`` records) is built into ONE buffer,
    written COMPLETELY to a private temp file (fully flushed + fsync'd),
    and only THEN published at the ledger's own path via ``os.link`` —
    which POSIX guarantees is atomic and fails with ``EEXIST`` if the
    target already exists. The ledger path itself is therefore NEVER
    observed in a "created but not yet fully written" state by anyone —
    it goes straight from "does not exist" to "exists with its FULL
    migrated content", because the content was already 100% durable
    before the ledger path was ever touched.

    A first cut of this fix used a plain exclusive-create
    (``O_CREAT | O_EXCL``) directly on the ledger path, then wrote the
    content in a SEPARATE ``write()`` call — durable-once-written, but
    NOT gap-free: between the create succeeding (ledger path now exists,
    still empty) and the write landing, a genuinely concurrent real
    decision could open the now-existing path in plain append mode,
    write its own line into that gap, and then this function's own
    (later) write — using ``O_APPEND``, which re-seeks to the CURRENT
    end of file at write time, not the offset at open time — would still
    land AFTER that real decision, reproducing the identical bug in a
    narrower window (one syscall pair instead of an entire per-key
    loop). The temp-file-then-link approach has no such window: nothing
    is ever written to a path any other process can observe until the
    content is already complete, so there is no gap to squeeze into.

    Every real decision in this codebase calls this function BEFORE its
    own append (``PermissionResolver._ensure_folded``, the CLI/web
    ``_load`` — see each caller's own docstring), so:
      - if THIS call wins the link, its full batch lands first, and
        only THEN does the caller proceed to append its own
        (necessarily later, necessarily correct) real decision;
      - if this call LOSES (``FileExistsError`` — someone else's
        migration, or a genuine append, already created the file), it
        is a no-op, and the caller's own subsequent real append still
        lands strictly AFTER whatever is already there.

    #5192: also where a pre-existing ledger's missing trailing newline
    gets repaired — see :func:`_repair_missing_trailing_newline`'s own
    docstring for why this is a migration-time, not a per-append, concern
    (``_write_record`` no longer checks this at all, by design)."""
    if ledger.path.exists():
        _repair_missing_trailing_newline(ledger.path)
        return
    if not legacy_yaml_path.exists():
        return
    try:
        import yaml
        data: Any = yaml.safe_load(legacy_yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(data, dict):
        return
    bound_section = data.get("_bound_identities")
    if not isinstance(bound_section, dict):
        bound_section = {}
    ts = ApprovalLedger._now_iso()
    lines: list[str] = []
    for key, value in data.items():
        if key == "_bound_identities" or not isinstance(value, bool):
            continue
        lines.append(json.dumps(
            {"ts": ts, "kind": "approval", "key": key, "approved": value},
            ensure_ascii=False,
        ))
    for key, entry in bound_section.items():
        if not isinstance(entry, dict) or "ino" not in entry:
            continue
        ino = entry.get("ino")
        if not isinstance(ino, int):
            continue
        bt = entry.get("birthtime")
        lines.append(json.dumps(
            {
                "ts": ts, "kind": "identity_bind", "key": key, "ino": ino,
                "birthtime": float(bt) if isinstance(bt, (int, float)) else None,
            },
            ensure_ascii=False,
        ))
    if not lines:
        return  # nothing valid to migrate -- don't create an empty ledger
    content = ("\n".join(lines) + "\n").encode("utf-8")
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    # Write the FULL batch to a private temp file first (fully durable —
    # flushed + fsync'd — before the ledger path is ever touched), then
    # publish it with os.link, which POSIX guarantees is atomic AND fails
    # with EEXIST if the target already exists — never a "created but not
    # yet written" intermediate state observable at the ledger path itself.
    # A plain O_CREAT|O_EXCL open on the ledger path directly (this
    # function's first cut) still leaves a 2-syscall gap between the
    # create succeeding and the content actually landing — a REAL
    # concurrent append could squeeze into exactly that gap (its own
    # migrate() attempt sees the now-existing-but-still-empty file, skips,
    # appends its real decision — landing BEFORE this migration's own
    # later write, reproducing the identical bug in a narrower window).
    # link() has no such gap: the content is complete before the ledger
    # path is ever created.
    #
    # os.link's atomicity is a SAME-FILESYSTEM guarantee -- a hard link
    # cannot cross filesystem boundaries at all (raises OSError/EXDEV).
    # The temp file MUST therefore be created in the SAME directory as
    # the ledger (never /tmp or any other mount point), which is why
    # `dir=str(ledger.path.parent)` below is load-bearing, not a
    # convenience default.
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=str(ledger.path.parent), prefix=".approvals-migrate-", suffix=".tmp",
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(str(tmp_path), str(ledger.path))
        except FileExistsError:
            pass  # someone else's migration (or a genuine first append) already won
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
