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
section) is migrated ONCE, on first touch, into day-0 records appended to
this ledger — see :func:`migrate_legacy_snapshot`. Folding the ledger
after migration must reproduce EXACTLY the same effective state the old
snapshot reader would have returned (the acceptance witness) — migration
adds records, it does not reinterpret them.

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
import time
from pathlib import Path
from typing import Any


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

    def append_approval(self, key: str, approved: bool) -> None:
        """Append one approval-decision record (a grant or a revoke).
        ``ts`` is DISPLAY-ONLY (see the module docstring's "Ordering
        authority" section) — :meth:`fold` never reads it."""
        self._write_record({
            "ts": self._now_iso(),
            "kind": "approval",
            "key": key,
            "approved": approved,
        })

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
        """Serialize *record* as one JSONL line, append, flush, fsync —
        identical shape to ``BudgetLedger._write_record``, including the
        leading-newline guard against a previous crash's partial
        (no-trailing-newline) write."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        need_lead = self._needs_lead_newline()
        with self._path.open("a", encoding="utf-8") as f:
            if need_lead:
                f.write("\n")
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _needs_lead_newline(self) -> bool:
        if not self._path.is_file():
            return False
        try:
            size = self._path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        try:
            with self._path.open("rb") as f:
                f.seek(-1, 2)
                return f.read(1) != b"\n"
        except OSError:
            return False

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
    ) -> "tuple[dict[str, bool], dict[str, tuple[int, float | None]]]":
        """Replay every record in file order into the two maps
        ``PermissionResolver`` needs: ``(approvals, bound_identities)``.

        Last ``"approval"`` record per key wins for the boolean; last
        ``"identity_bind"`` record per key wins for the identity — EXCEPT
        an ``"approval"`` record with ``approved=False`` also clears that
        key's bound identity (see the module docstring's "Fold semantics"
        section for why this mirrors #5157, not a new rule)."""
        approvals: "dict[str, bool]" = {}
        bound: "dict[str, tuple[int, float | None]]" = {}
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
                if not approved:
                    bound.pop(key, None)
            elif kind == "identity_bind":
                ino = rec.get("ino")
                if not isinstance(ino, int):
                    continue
                bt = rec.get("birthtime")
                bound[key] = (ino, float(bt) if isinstance(bt, (int, float)) else None)
        return approvals, bound


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
    malformed ``_bound_identities`` entry is skipped, never an error)."""
    if ledger.path.exists() or not legacy_yaml_path.exists():
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
    for key, value in data.items():
        if key == "_bound_identities" or not isinstance(value, bool):
            continue
        ledger.append_approval(key, value)
    for key, entry in bound_section.items():
        if not isinstance(entry, dict) or "ino" not in entry:
            continue
        ino = entry.get("ino")
        if not isinstance(ino, int):
            continue
        bt = entry.get("birthtime")
        ledger.append_identity_bind(
            key, ino, float(bt) if isinstance(bt, (int, float)) else None,
        )
