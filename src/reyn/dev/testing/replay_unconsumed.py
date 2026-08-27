"""Structural gate: an ``LLMReplay`` fixture entry nobody's REPLAY ever
consumed this session, reported by measurement, not by enumeration (#5283).

The problem this closes
------------------------
#3634 made in-place regeneration REPLACE a call's stale entry instead of
appending — but ``replay_stacking.group_signature`` groups on everything the
key hashes over EXCEPT ``tools``, because ``tools`` is the one component a
schema change is *expected* to move. When reyn's OWN code changes what it
injects into a message instead (an ``EMPTY_STOP_RETRY_DIRECTIVE``, a
``G12_SIGNAL_TEXT`` — #5273 is one real instance), the per-message digest
moves, ``group_signature`` moves with it, and the old entry is no longer
recognised as "the same call, an earlier generation" — it just sits on disk
forever, and the fixture matches both the old and the new message content,
never going RED regardless of which one the code actually sends (#5283,
architect, e2e-coder's real-execution repro on the issue).

Two static answers were proposed and both rejected (#5283, architect's
ruling, lead-coder's accept): truncate-on-regenerate answers correctly but
conflicts with #3634's own reason for choosing replace-over-truncate — a
fixture file legitimately holds entries several SIBLING tests share
(``replay.py``'s own docstring, 3 measured cases) and a truncate would erase
them unless every sibling test ran in the SAME regenerate pass. Excluding
reyn's injected tokens from ``group_signature`` answers correctly today but
needs a closed enumeration of every token reyn will ever inject, which
cannot be proven closed — a new token reopens the identical hole for
whichever token wasn't on the list.

The shape here answers a different question: not "which entries COULD this
change reach" (static, needs enumeration or an equally strong assumption)
but "which entries did nothing in this session actually reach" (a
measurement, needs nothing enumerated). ``LLMReplay._replay``/
``_replay_embedding`` now record every key a real replay hit actually
served (``LLMReplay.consumed_keys()``); this module diffs that against
every key the SAME fixture file held on disk (``LLMReplay.loaded_keys()``)
and reports the remainder. A new injected token moves a key, the old key's
entry stops being consumed, and it falls into "unconsumed" automatically —
no one has to know the token existed. #3969's ``kind="environment"`` entries
sit outside this (they carry no ``key``, only ``name``, and this module does
not attempt to cover them) — a distinct #5283 follow-up territory, not this
PR's scope.

What this does NOT do (disclosed, per architect's ruling)
-----------------------------------------------------------
This is DETECTION, not PREVENTION. A stacked/orphaned entry keeps sitting on
disk, silently matching both generations, until someone runs the check and
reads the report. Nothing here stops the entry from being WRITTEN — #3634's
own append-vs-replace fix already does that for same-``group_signature``
in-place regeneration; this module is the check for the case that fix's own
grouping rule structurally cannot see. lead-coder's accept condition ruled
this acceptable: the owner's standing instruction is "close the class," not
"make it unconstructible" (owner's clean-end-state principle, applied here
as: cheapest correct closure, not the strongest possible one) — #3634's own
sibling-shared-fixture design is a real, deliberate cost that a
prevention-shaped fix (truncate) would have to re-pay.

Three conditions for this to mean anything (architect's ruling, verbatim
requirements, not this module's own invention)
------------------------------------------------------------------------------
1. **Only a genuinely full run can say "unconsumed."** ``-k``, ``-x``, a
   single-file invocation, or any other narrowing can leave a sibling test
   that WOULD have consumed an entry simply not run — that entry is not
   unreachable, it is unvisited. Inferring "no filter flag was passed, so
   this must be a full run" is the exact same unprovable-closure mistake the
   excluded-tokens option made. So the check is FAIL-OPEN by construction:
   it does nothing unless an explicit, positive signal
   (``REYN_REPLAY_UNCONSUMED_CHECK=1``) says this run IS the full run —
   never inferred from what was absent.
2. **xdist workers must be aggregated, not judged individually.** Each
   worker only ever sees the tests IT was handed; a worker whose shard
   happened to run zero consumers of some entry would falsely report it
   unconsumed if judged alone. Every worker appends
   "opened"/"consumed" events to one shared JSONL file (the same technique
   ``network_gate``'s cross-worker ``stale_allow_markers`` uses, for the
   identical reason); only the process that owns the whole session (the
   xdist controller, or the sole process when xdist is not in use — the one
   without ``session.config.workerinput``) reads it back and judges.
3. **A skip is not an unreachable entry.** A skipped test's fixture never
   got a chance to consume anything, and that is not evidence the entry is
   dead — it is evidence this run's environment narrowed what could run.
   ``pytest_sessionfinish`` checks ``session.testscollected`` /
   ``session.testsfailed`` are not the mechanism here (a skip reason is not
   visible from THIS file's own bookkeeping); this module instead reports
   the session's skip COUNT alongside any unconsumed finding, so a reader
   can see whether the run that produced the finding also skipped anything
   the finding's fixture file's owning tests could plausibly need — and, per
   the ruling, an env var (``REYN_REPLAY_UNCONSUMED_CHECK``) is meant to be
   set only in a CI job that installs every extra (no skip source), never as
   a blanket default for every local run.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_CHECK_ENV_VAR = "REYN_REPLAY_UNCONSUMED_CHECK"


def _events_path() -> Path:
    return Path(
        os.environ.get(
            "REYN_REPLAY_UNCONSUMED_EVENTS_PATH",
            str(Path(tempfile.gettempdir()) / "reyn_replay_unconsumed_events.jsonl"),
        )
    )


def reset_events_file() -> None:
    """Truncate the shared events file. Call once, from the process that owns
    the whole session (the xdist controller, or the sole process otherwise)."""
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def report_instance(fixture_path: "str", loaded: "set[str]", consumed: "set[str]") -> None:
    """Append this ``LLMReplay`` instance's own (loaded, consumed) keys for
    *fixture_path* to the shared events file. Called from
    ``tests/conftest.py``'s ``_llm_replay`` fixture teardown, replay mode
    only — record mode answers a different question (#3634's own dedup), not
    this one.

    One "opened" event marks that this session touched *fixture_path* at
    all (so a file this session never opened is correctly excluded from the
    report rather than reported as 100% unconsumed); "loaded"/"consumed"
    events carry that instance's own key sets. Every LLMReplay instance
    against the same file reports the SAME ``loaded`` set (the file's
    content does not change under replay mode), so a union at read time is
    idempotent regardless of how many tests opened it.

    No-op unless ``REYN_REPLAY_UNCONSUMED_CHECK=1`` — every OTHER local run
    pays zero cost for a mechanism it never asked to run (fail-open extends
    to the write side too, not just the read side pytest_sessionfinish
    gates).
    """
    if os.environ.get(_CHECK_ENV_VAR) != "1":
        return
    with _events_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "opened", "fixture": fixture_path}) + "\n")
        for key in sorted(loaded):
            fh.write(json.dumps({"kind": "loaded", "fixture": fixture_path, "key": key}) + "\n")
        for key in sorted(consumed):
            fh.write(json.dumps({"kind": "consumed", "fixture": fixture_path, "key": key}) + "\n")


def unconsumed_by_fixture() -> "dict[str, set[str]]":
    """``{fixture_path: {unconsumed keys}}`` for every fixture this session
    opened in replay mode, aggregated across every event this shared file
    holds (i.e. across every xdist worker) — a file this session never
    opened is silently absent, never reported as fully unconsumed.

    #5283 witness 2 (architect's ruling): if ``LLMReplay``'s own consumption
    recording (``_consumed_keys.add`` in ``_replay``/``_replay_embedding``)
    is itself broken — stripped, or raising before it reaches the ``add``
    call — every consumed set collapses to empty while loaded sets stay
    populated, which would otherwise flood this report with a FALSE claim
    that every single entry in every opened fixture is unreachable. That is
    the wrong failure direction: "the detector died" and "everything is
    unreachable" must not look identical, or a broken detector silently
    becomes indistinguishable from (and gets acted on as) real evidence. A
    real, passing replay-mode test can only reach a fixture entry through
    ``_replay``/``_replay_embedding``'s own hit path — a fixture with ANY
    loaded key that this session opened but for which NOTHING anywhere
    consumed ANYTHING is not plausible unless the recorder itself is dead,
    so that global state suppresses the report entirely rather than naming
    a remainder.
    """
    events_path = _events_path()
    if not events_path.exists():
        return {}

    opened: "set[str]" = set()
    loaded: "dict[str, set[str]]" = {}
    consumed: "dict[str, set[str]]" = {}
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except Exception:
            continue
        kind = event.get("kind")
        fixture = event.get("fixture")
        if fixture is None:
            continue
        if kind == "opened":
            opened.add(fixture)
        elif kind == "loaded":
            loaded.setdefault(fixture, set()).add(event["key"])
        elif kind == "consumed":
            consumed.setdefault(fixture, set()).add(event["key"])

    total_loaded = sum(len(v) for v in loaded.values())
    total_consumed = sum(len(v) for v in consumed.values())
    if total_loaded > 0 and total_consumed == 0:
        # The recorder-dead canary above — every opened fixture would show
        # up as 100% unconsumed, which is the false-positive-flood shape
        # this function must never produce.
        return {}

    result: "dict[str, set[str]]" = {}
    for fixture in opened:
        remainder = loaded.get(fixture, set()) - consumed.get(fixture, set())
        if remainder:
            result[fixture] = remainder
    return result


# ── pytest plugin wiring ─────────────────────────────────────────────────────
#
# Same shape as ``network_gate``'s own wiring, for the same reason: a
# `pytester`-driven test (``tests/dev/test_replay_unconsumed_5283.py``) can
# exercise these hooks in a real, isolated inner pytest session.


def pytest_configure(config: "pytest.Config") -> None:
    if os.environ.get(_CHECK_ENV_VAR) != "1":
        return
    if not hasattr(config, "workerinput"):
        # The xdist controller, or the sole process when xdist is not in
        # use — the one process allowed to reset the cross-worker events
        # file (mirrors network_gate.pytest_configure's identical guard).
        reset_events_file()


def pytest_sessionfinish(session: "pytest.Session", exitstatus: int) -> None:
    """Report unconsumed entries and fail the run (#5283 accept condition,
    lead-coder: "a report nobody's required to read is not a mechanism").

    Fail-open by construction: does nothing at all unless
    ``REYN_REPLAY_UNCONSUMED_CHECK=1`` is set — never inferred from the
    absence of ``-k``/``-x``/a narrowed invocation, which would repeat the
    exact unprovable-closure mistake the excluded-injected-tokens option
    made (architect's ruling, condition 1).
    """
    if os.environ.get(_CHECK_ENV_VAR) != "1":
        return
    if hasattr(session.config, "workerinput"):
        return

    hits = unconsumed_by_fixture()
    if not hits:
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped_count = len(reporter.stats.get("skipped", [])) if reporter is not None else 0

    lines = [
        "",
        f"UNCONSUMED LLMReplay fixture entries (#5283) — {sum(len(v) for v in hits.values())} "
        f"key(s) across {len(hits)} file(s) never served a replay hit this session:",
    ]
    for fixture in sorted(hits):
        for key in sorted(hits[fixture]):
            lines.append(f"  - {fixture}: {key}")
    if skipped_count:
        lines.append(
            f"\nNOTE: {skipped_count} test(s) were SKIPPED this session — a skip is "
            "not evidence an entry is unreachable, only that this run's environment "
            "narrowed what could run. Re-run in an environment with every extra "
            "installed before treating this report as final (architect's ruling, "
            "condition 3)."
        )
    lines.append(
        "\nEach entry above exists in its fixture file but was never returned by a "
        "replay hit anywhere in this session. If it is a stale generation left behind "
        "by an in-place regenerate (#5283/#3634), delete it and re-run the owning "
        "test(s) to confirm nothing still needs it."
    )
    message = "\n".join(lines)

    if reporter is not None:
        reporter.write_line(message, red=True)
    else:
        print(message)  # noqa: T201 — no terminalreporter (e.g. -q -s edge case)
    session.exitstatus = 1
