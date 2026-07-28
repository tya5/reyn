"""reyn.hooks.durable_pending_store — the crash-durable ``PendingStore``
implementation behind the Composer's ``deadline`` (dead-man) op, issue #3180.

``InMemoryPendingStore`` (``reyn.hooks.composer``) is best-effort: a process
crash silently discards every in-flight correlation. For six of the eight
Composer ops that costs at most one buffered notification. For ``deadline`` it
costs **the monitoring itself** — the watchdog disappears together with the
thing it was watching, since whatever it was armed on is very likely inside the
same crash. :class:`DurablePendingStore` is the same
:class:`~reyn.hooks.composer.PendingStore` protocol backed by a full-state JSON
file, so an armed deadline survives a restart **with its original arm instant**
and fires (or has already missed its window and fires on the first sweep).

Why a full-state snapshot file and NOT WAL-events
-------------------------------------------------
The obvious reading of "make it durable" in this repo is "write WAL-events and
replay them". That shape is precisely the #2259 data-loss class: the WAL is
truncated below ``floor = min(agent applied_seq)``, so state derived ONLY from
WAL-events that fell below the floor is silently lost at reconstruct. A
dead-man switch that silently fails to re-arm is the worst instance of it.

So this store follows the same resolution ``ConfigGenerationStore`` (#2259 PR-1)
adopted: the pending set is already FULL state (a small dict of per-key
records), so it IS a snapshot. It is written as a file, not as truncatable
WAL-events, and reconstruction reads that file alone — the WAL is never
consulted. That makes CLAUDE.md's truncate-falsify gate pass **structurally**
rather than by argument (``tests/test_3180_composer_pending_truncate_falsify.py``
sets an armed deadline, truncates the WAL below every event of the run, rebuilds
from this file, and asserts the arm survives with its ``created_at`` intact).

Why the arm INSTANT is the load-bearing field
---------------------------------------------
A deadline's semantic is ``armed_at + ttl``. Restoring "j1 is being watched"
without ``armed_at`` — the shape any boot-time re-arm from a job registry would
produce — restarts the clock, so the deadline silently slides forward by the
whole crash + downtime window. That is exactly the interval the dead-man switch
exists to notice, so a re-armed-but-reset monitor is worse than a missing one:
it reports healthy. :meth:`DurablePendingStore.get` therefore returns records
whose ``created_at`` is the ORIGINAL arm time, and the crash test asserts on
that field rather than on mere presence.

Failure posture (never a silent dead-man)
-----------------------------------------
Both failure legs are loud, matching ``load_composers``' load-time
``UserWarning``: an unreadable/corrupt store file logs an error and starts
empty (the operator learns the armed set was lost rather than silently watching
nothing), and a failed write logs an error naming the composer + key whose
durability just lapsed. Neither raises — a store fault must not take down the
bus-consuming Composer loop that is still delivering every other op.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path

from reyn.hooks.composer import PendingRecord
from reyn.hooks.event import HookEvent

_log = logging.getLogger(__name__)

# Bumped only on an incompatible on-disk shape change. A file whose version
# this build does not understand is treated exactly like a corrupt one (loud,
# start-empty) rather than best-effort-parsed: half-decoded arm state would
# re-introduce the silent-wrong-deadline failure this module exists to remove.
_SCHEMA_VERSION = 1

STORE_FILENAME = "composer_pending.json"


def _encode(record: PendingRecord) -> dict:
    return {
        "events": [dataclasses.asdict(e) for e in record.events],
        "matched_inputs": sorted(record.matched_inputs),
        "seq_pos": record.seq_pos,
        "created_at": record.created_at,
        "last_at": record.last_at,
    }


def _decode(raw: dict) -> PendingRecord:
    return PendingRecord(
        events=[HookEvent(**e) for e in raw["events"]],
        matched_inputs=set(raw["matched_inputs"]),
        seq_pos=raw["seq_pos"],
        created_at=raw["created_at"],
        last_at=raw["last_at"],
    )


class DurablePendingStore:
    """A crash-durable :class:`~reyn.hooks.composer.PendingStore` — the whole
    pending set as one atomically-rewritten JSON file.

    Every mutation rewrites the file (tmp + ``fsync`` + ``os.replace`` + a
    parent-directory ``fsync`` — see :meth:`_fsync_parent_dir` for why the last
    one is required rather than optional), so the file on disk is always a
    complete, self-consistent pending set: there is no partial-apply window and
    no replay step. That write amplification is why
    ``ComposerDef.durable`` defaults to True only for ``deadline`` — arm/disarm
    edges are rare, whereas a ``debounce`` composer's per-event churn would pay
    an fsync per bus event for a guarantee that op does not need.
    """

    def __init__(self, path: "Path | str") -> None:
        self._path = Path(path)
        self._data: "dict[tuple[str, str], PendingRecord]" = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # -- reconstruction -------------------------------------------------

    def _load(self) -> None:
        """Rebuild the pending set from the store file. The ONLY reconstruction
        source — the WAL is never read here (see module docstring: WAL-derived
        recovery state is the #2259 silent-loss class)."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return  # first run for this session — nothing was ever armed
        except (OSError, ValueError) as exc:
            _log.error(
                "Composer pending store %s is unreadable (%s) — starting with NO armed "
                "state. Any deadline (dead-man) composer that was armed before this "
                "restart will not fire.", self._path, exc,
            )
            return
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            _log.error(
                "Composer pending store %s has an unrecognised schema version %r "
                "(this build reads %d) — starting with NO armed state.",
                self._path, (raw or {}).get("version") if isinstance(raw, dict) else None,
                _SCHEMA_VERSION,
            )
            return
        for entry in raw.get("records", []):
            try:
                self._data[(entry["composer"], entry["key"])] = _decode(entry["record"])
            except (KeyError, TypeError, ValueError) as exc:
                _log.error(
                    "Composer pending store %s: dropping an undecodable record (%s) — "
                    "if it was an armed deadline, it will not fire.", self._path, exc,
                )

    # -- durability -----------------------------------------------------

    def _flush(self) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "records": [
                {"composer": composer, "key": key, "record": _encode(record)}
                for (composer, key), record in sorted(self._data.items())
            ],
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self._path)
        except OSError as exc:
            # Loud, never silent: the in-memory set is still correct for THIS
            # process, but the crash guarantee has lapsed for every record in it.
            _log.error(
                "Composer pending store %s: durable write failed (%s) — the armed set "
                "is no longer crash-durable and will be lost if this process dies.",
                self._path, exc,
            )
            return
        self._fsync_parent_dir()

    def _fsync_parent_dir(self) -> None:
        """Persist the ``os.replace`` rename itself, not just the file contents.

        This is a deliberate durability-level decision, not boilerplate. Paying
        ``fsync`` on the tmp file above already commits this store to a threat
        model STRONGER than a process crash — a process crash cannot lose the
        page cache, so against that threat alone no fsync would be needed at
        all. Under the stronger threat (power loss / kernel panic) a directory
        entry created by ``rename`` is not durable until the PARENT DIRECTORY
        is fsync'd, so without this the contents would survive while the rename
        that makes them visible is lost — leaving the PREVIOUS generation of
        the file in place.

        The loss is asymmetric, which is what makes it worth the syscall:
        losing the last **arm** means a dead-man monitor that silently never
        fires — precisely the failure this module exists to remove — whereas
        losing the last **disarm** only costs a spurious missed-deadline fire
        (noisy, but the safe direction).

        **This guarantee is not provable by a test in this suite.** A
        process-crash test cannot distinguish a dir-fsync'd rename from a
        non-fsync'd one (both survive), so there is deliberately no gate
        asserting it — inventing one would manufacture a green result for a
        property the test never exercised. It is argued from the write path,
        not witnessed.

        Failure here is logged at DEBUG, not ERROR, and never re-reports the
        write as failed: the data IS in the page cache and correct for this
        process, and on platforms where opening a directory is not permitted
        (Windows) this would otherwise fire a false alarm on every arm."""
        try:
            fd = os.open(self._path.parent, os.O_RDONLY)
        except OSError as exc:
            _log.debug(
                "Composer pending store %s: parent-directory fsync unavailable (%s) — the "
                "rename's durability against power loss is unconfirmed on this platform.",
                self._path, exc,
            )
            return
        try:
            os.fsync(fd)
        except OSError as exc:
            _log.debug(
                "Composer pending store %s: parent-directory fsync failed (%s) — the "
                "rename's durability against power loss is unconfirmed.", self._path, exc,
            )
        finally:
            os.close(fd)

    # -- PendingStore protocol ------------------------------------------

    def get(self, composer: str, key: str) -> "PendingRecord | None":
        return self._data.get((composer, key))

    def put(self, composer: str, key: str, record: PendingRecord) -> None:
        self._data[(composer, key)] = record
        self._flush()

    def delete(self, composer: str, key: str) -> None:
        if self._data.pop((composer, key), None) is not None:
            self._flush()

    def keys(self, composer: str) -> "list[str]":
        return [k for (c, k) in self._data if c == composer]

    # -- housekeeping ---------------------------------------------------

    def retain_composers(self, names: "set[str]") -> int:
        """Drop restored records belonging to composers that no longer exist in
        config, returning the count dropped. Without this a renamed/removed
        ``deadline`` composer's arm state would live in the file forever —
        unbounded growth on the cross-cutting band's cost/budget leg, and a
        record that can never be disarmed because nothing consumes its key."""
        stale = [k for k in self._data if k[0] not in names]
        for key in stale:
            del self._data[key]
        if stale:
            self._flush()
        return len(stale)


__all__ = ["STORE_FILENAME", "DurablePendingStore"]
