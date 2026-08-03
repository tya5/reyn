"""Structural gate: no unpinned real-network reach through reyn's LLM/embedding
funnel (#3451 part 2 — the "closure" half of #3445's coverage fix).

Boundary, not enumeration
-------------------------
#3445 measured 51 tests without ``@pytest.mark.replay`` reaching a real
``litellm`` network boundary — most alarmingly, 38 of them never even
surfaced as a visible FAILED test (the exception was swallowed somewhere
inside a background run-loop / best-effort embed path). Extending
``LLMReplay`` to ``litellm.aembedding`` (#3451 part 1) closes the *known*
25-test gap, but patching one more named method only closes THIS hole — if
litellm grows a new async surface (``atranscription``, ``arerank``, ...)
tomorrow and reyn's own code starts calling it, the identical blind spot
reopens silently, and #3445 already showed silent reopening is the dangerous
failure mode.

``LLM_NETWORK_BOUNDARY_ATTRS`` is therefore not a hand-maintained guess at
"which litellm functions might reach the network" — it is the SSoT for which
top-level ``litellm.<attr>`` coroutine functions reyn's OWN source code
actually calls, kept honest bidirectionally by
``tests/test_network_gate_boundary_completeness_3451.py`` (mirroring #3437's
SSoT + bidirectional-gate shape):

- **declared ⊆ real**: every name in this tuple is a real coroutine attribute
  of the ``litellm`` module (catches a stale/renamed entry).
- **real ⊆ declared**: every ``litellm.<attr>(`` call site in ``src/reyn``
  where ``attr`` resolves to a coroutine function on the ``litellm`` module is
  in this tuple (catches reyn's own code reaching a NEW litellm async surface
  this gate does not cover yet — that PR fails the completeness test until it
  patches the gate too, same PR, not a follow-up: CLAUDE.md's
  doc-goes-stale-the-moment-the-mechanism-changes rule, applied to a runtime
  gate instead of a doc).

What the gate does
------------------
Installed once per pytest session (``pytest_configure``, wired from
``tests/conftest.py``). Every attribute named in
``LLM_NETWORK_BOUNDARY_ATTRS`` is wrapped. A call reaching the wrapper means
one of two things happened:

1. The test is ``@pytest.mark.replay``-pinned AND in replay mode:
   ``LLMReplay.install()`` has already REPLACED this wrapper for the test's
   duration (see ``tests/conftest.py::_llm_replay``) — the wrapper is not
   even in the call path, so this module never sees the call. (In replay
   mode a cache miss raises ``MissingFixture`` from ``LLMReplay`` itself,
   never reaching a real socket either.)
2. The test is ``@pytest.mark.replay``-pinned AND ``REYN_LLM_RECORD=1`` is
   set (record mode): ``LLMReplay._record()``/``_record_embedding()`` call
   back into what they captured as "the original litellm.<attr>" — which,
   with this gate installed FIRST (``pytest_configure`` runs before any
   test's ``_llm_replay`` fixture), IS this wrapper. The wrapper checks
   ``os.environ.get("REYN_LLM_RECORD") == "1"`` directly and forwards to the
   real litellm function — the ONE case #3451 built this bootstrap path for.
   #3662 correction: earlier, ``@pytest.mark.replay`` PRESENCE alone (marker
   check, not the env var) was treated as this same authorization — on the
   theory that a fixture file merely being absent was equivalent to an
   operator-invoked recording. It was not: #3451's own bootstrap reason is
   about record mode calling itself, not about why a fixture is missing, and
   a missing/deleted/corrupted fixture is not evidence of operator intent.
   That path let a real, unpinned call through silently (#3660/#3662).
3. The test is NOT replay-pinned (or is replay-pinned but not currently
   recording). The wrapper checks whether the currently running test carries
   ``@pytest.mark.allow_real_network(reason=...)``. If
   not, it raises ``UnpinnedNetworkReach`` instead of letting the call reach
   a real socket — turning #3445's silent 38 into a loud, attributable
   failure. If it does carry the marker, the call is forwarded to the real
   litellm function (the two loopback-only exception classes #3445 found:
   B — a refused port pinning a proxy-routing decision; D — a real
   litellm client driven against a local stalled/refused socket to test
   retry/timeout/cancel bounds, which ``@replay`` would delete the very
   behaviour under test).

Declaring ``allow_real_network`` without a ``reason=`` is itself a gate
failure (an unexplained exception is as bad as an undocumented one). Declaring
it on a test that then never actually triggers a real call this session is
ALSO a gate failure — ``stale_allow_markers()`` implements the #3437 "declared
⊆ actual" direction for the exception registry itself: an exception nobody
exercises is unlimited silent permission, not a documented carve-out.
"""
from __future__ import annotations

import functools
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

# ── SSoT (kept honest by tests/test_network_gate_boundary_completeness_3451.py) ─

LLM_NETWORK_BOUNDARY_ATTRS: tuple[str, ...] = ("acompletion", "aembedding")

ALLOW_MARKER_NAME = "allow_real_network"


class UnpinnedNetworkReach(RuntimeError):
    """A non-@replay, non-@allow_real_network test reached a real litellm
    network boundary (#3445 / #3451)."""


# ── Per-process state (one instance per pytest-xdist worker, or the single
# process when xdist is not in use) ─────────────────────────────────────────

_current_nodeid = "<no test running>"
_current_node: "pytest.Item | None" = None
_installed = False
_originals: dict[str, Any] = {}

# Shared across xdist workers via a plain-append JSONL file — the only way
# for the controller's stale-marker check (module-level API below) to see
# what every worker process observed. Cleared once at session start by
# whichever process runs pytest_configure first without a workerinput (the
# controller, or the sole process when xdist is not in use); workers only
# ever append, never truncate, so a worker spawned after configure cannot
# race the clear.
#
# Resolved fresh from the env var on every call (not frozen at import time)
# so a test can point a nested pytest session at an isolated events file via
# ``monkeypatch.setenv`` — see tests/test_network_gate_3451.py, which drives
# exactly this via an in-process `pytester` run sharing this same module.
def _events_path() -> Path:
    return Path(
        os.environ.get(
            "REYN_NETWORK_GATE_EVENTS_PATH",
            str(Path(tempfile.gettempdir()) / "reyn_network_gate_events.jsonl"),
        )
    )


def _append_event(kind: str, nodeid: str) -> None:
    with _events_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, "nodeid": nodeid}) + "\n")


def reset_events_file() -> None:
    """Truncate the shared events file. Call once, from the process that owns
    the whole session (the xdist controller, or the sole process otherwise)."""
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def install(litellm_module: Any) -> None:
    """Wrap every ``LLM_NETWORK_BOUNDARY_ATTRS`` name on ``litellm_module``.

    Idempotent per-process (a second call is a no-op) — pytest-xdist workers
    each import this module fresh, so "idempotent" only needs to hold within
    one process, not across the whole run.
    """
    global _installed
    if _installed:
        return
    for attr in LLM_NETWORK_BOUNDARY_ATTRS:
        original = getattr(litellm_module, attr)
        _originals[attr] = original
        setattr(litellm_module, attr, _make_gate(attr, original))
    _installed = True


def _make_gate(attr: str, original: Any):
    @functools.wraps(original)
    async def _gate(*args: Any, **kwargs: Any) -> Any:
        node = _current_node
        if os.environ.get("REYN_LLM_RECORD") == "1":
            # #3451 record-mode bootstrap: `LLMReplay._record()` calls back
            # into what it captured as "the original litellm.<attr>" — which,
            # with this gate installed, IS this wrapper (install() runs before
            # any test's `_llm_replay` fixture). Without letting THIS ONE
            # signal through, an operator-invoked recording run would block
            # itself. `REYN_LLM_RECORD=1` is that signal: the operator typed
            # it, so it is an intentional, operator-invoked real call.
            #
            # #3662: this used to ALSO treat `node.get_closest_marker("replay")
            # is not None` as authorization on its own — on the theory that a
            # test merely CARRYING the marker, with its fixture file missing,
            # was equivalent to an operator-invoked recording (the comment
            # here used to read "REYN_LLM_RECORD=1, or a missing fixture
            # file"). It was not: the bootstrap constraint above is about
            # RECORD MODE calling itself, not about a fixture happening to be
            # absent. A test can carry `@pytest.mark.replay` and have its
            # fixture deleted or corrupted by an accident that has nothing to
            # do with operator intent (#3660's fixture-dependence audit
            # deleted one to test it) — the marker-based check let that real
            # call through silently, swallowed by litellm's own retry/backoff,
            # the test staying green. Of #3451's one named reason (the
            # bootstrap self-block above), only the `REYN_LLM_RECORD=1` half
            # was ever actually derived from it; "a missing fixture file" was
            # listed alongside it without its own justification. Gating on
            # the explicit env var only closes that half.
            return await original(*args, **kwargs)

        marker = node.get_closest_marker(ALLOW_MARKER_NAME) if node is not None else None
        if marker is None:
            raise UnpinnedNetworkReach(
                f"litellm.{attr} reached with no @pytest.mark.replay pin and no "
                f"@pytest.mark.allow_real_network(reason=...) — test={_current_nodeid}. "
                "Pin it (LLMReplay now covers both acompletion and aembedding, "
                "#3451) or, if the real litellm client against a LOOPBACK-only "
                "endpoint is the point of the test, mark it allow_real_network "
                "with a reason. See #3445 / #3451."
            )
        reason = marker.kwargs.get("reason") or (marker.args[0] if marker.args else None)
        if not reason:
            raise UnpinnedNetworkReach(
                f"@pytest.mark.allow_real_network on {_current_nodeid} has no "
                "reason= — every explicit real-network exception must say why "
                "(#3451: an unexplained exception is as bad as an undeclared one)."
            )
        _append_event("triggered", _current_nodeid)
        return await original(*args, **kwargs)

    return _gate


# ── Per-test bookkeeping (wired from tests/conftest.py hooks) ───────────────


def note_test_start(item: "pytest.Item") -> None:
    global _current_nodeid, _current_node
    _current_nodeid = item.nodeid
    _current_node = item
    if item.get_closest_marker(ALLOW_MARKER_NAME) is not None:
        _append_event("declared", item.nodeid)


def note_test_end() -> None:
    global _current_nodeid, _current_node
    _current_nodeid = "<between tests>"
    _current_node = None


def stale_allow_markers() -> set[str]:
    """Nodeids that declared ``allow_real_network`` but never actually
    triggered a real litellm call anywhere in this session (across every
    xdist worker, via the shared events file) — the #3437 "declared ⊆
    actual" direction: an exception nobody exercises is a silent, unlimited
    permission slip, not a documented carve-out.
    """
    declared: set[str] = set()
    triggered: set[str] = set()
    events_path = _events_path()
    if not events_path.exists():
        return declared
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except Exception:
            continue
        if event.get("kind") == "declared":
            declared.add(event["nodeid"])
        elif event.get("kind") == "triggered":
            triggered.add(event["nodeid"])
    return declared - triggered


# ── pytest plugin wiring ─────────────────────────────────────────────────────
#
# This module IS a pytest plugin (declared via `pytest_plugins =
# ["reyn.dev.testing.network_gate"]` in tests/conftest.py) rather than logic
# folded into that conftest — so tests/test_network_gate_3451.py can drive a
# real, isolated inner pytest session (`pytester` + `pytest_plugins =
# ["reyn.dev.testing.network_gate"]`, no other fixtures) to exercise these
# hooks end to end without depending on this repo's other, layout-specific
# conftest fixtures.


def pytest_configure(config: "pytest.Config") -> None:
    config.addinivalue_line(
        "markers",
        "allow_real_network(reason): declare that this test DELIBERATELY drives "
        "a real litellm.acompletion/aembedding call with no @replay pin (#3451). "
        "reason= is required. Every reach through litellm's network boundary "
        "with neither @replay nor this marker fails the session "
        "(reyn.dev.testing.network_gate.UnpinnedNetworkReach) — and a marker "
        "that never actually triggers a real call this session is itself a "
        "gate failure (see pytest_sessionfinish below).",
    )

    if os.environ.get("REYN_DISABLE_NETWORK_GATE") == "1":
        return

    if not hasattr(config, "workerinput"):
        # The xdist controller, or the sole process when xdist is not in
        # use — the one process allowed to reset the cross-worker events
        # file (see the module docstring for why this must not race a
        # worker's own pytest_configure).
        reset_events_file()

    import litellm

    install(litellm)


def pytest_runtest_setup(item: "pytest.Item") -> None:
    note_test_start(item)


def pytest_runtest_teardown(item: "pytest.Item", nextitem: "pytest.Item | None") -> None:
    note_test_end()


def pytest_sessionfinish(session: "pytest.Session", exitstatus: int) -> None:
    """The #3437-shaped "declared ⊆ actual" direction for `allow_real_network`:
    a marker that never triggered a real litellm call this session is a stale,
    unlimited-in-practice permission slip, not a documented exception — fail
    the run. Only the process that owns the whole session checks (the xdist
    controller, or the sole process when xdist is not in use) — each worker
    only ever knows about the tests IT ran; the shared events file is what
    lets the controller see the whole picture."""
    if os.environ.get("REYN_DISABLE_NETWORK_GATE") == "1":
        return
    if hasattr(session.config, "workerinput"):
        return

    stale = stale_allow_markers()
    if stale:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = (
            "\nSTALE @allow_real_network marker(s) — declared but never "
            "triggered a real litellm call this session (#3451):\n  "
            + "\n  ".join(sorted(stale))
        )
        if reporter is not None:
            reporter.write_line(message, red=True)
        else:
            print(message)  # noqa: T201 — no terminalreporter (e.g. -q -s edge case)
        session.exitstatus = 1
