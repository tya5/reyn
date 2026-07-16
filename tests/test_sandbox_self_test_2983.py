"""Tier 2b: the sandbox enforcement self-test (#2983 stage 1) — a backend that
reports itself available must have actually fired a deny.

The invariant under test is NOT "the self-test exists". Every sandbox layer reyn
ships existed, and all three enforced nothing while `available()` said True. So
the first thing these tests establish is that the probe CAN FAIL: pointed at a
real backend that genuinely does not enforce, it must say so. A probe that cannot
fail is decoration, and decoration is not evidence — it just makes the suite
green, which is exactly the state #2962 / #2980 / #2978 were all discovered in.

`NoopBackend` is the falsification vehicle, and deliberately so: it is a REAL,
in-repo, production backend whose documented contract is "no isolation enforced".
No fake is needed to construct a non-enforcing backend — we ship one. It is also
always available on every platform, so the witness below is not platform-gated
and cannot silently degrade into a skip.
"""
from __future__ import annotations

import logging
import platform
import sys

import pytest

from reyn.config import SandboxConfig
from reyn.security.sandbox import NoopBackend, get_default_backend
from reyn.security.sandbox.self_test import (
    _reset_cache_for_tests,
    enforcement_self_test,
    probe_enforcement,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The probe cache is process-global and keyed on backend name, so a result
    from one test would otherwise be served to the next."""
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# ─── 1. ★ The witness: the probe catches a backend that cannot deny ───────────


def test_probe_catches_a_backend_that_does_not_enforce():
    """Tier 2b: ★ the self-test's own falsification — `probe_enforcement` pointed
    at NoopBackend (a real backend that enforces nothing) MUST report a failure.

    This is the test that gives every other assertion in this file its meaning.
    If it passed vacuously — if the probe could not distinguish a real sandbox
    from a passthrough — then a green self-test would carry no information, which
    is precisely the condition all three sandbox layers were broken in.
    """
    reason = probe_enforcement(NoopBackend())

    assert reason is not None, (
        "probe_enforcement() reported NoopBackend as ENFORCING. NoopBackend runs "
        "commands with no isolation whatsoever — if the probe cannot catch that, "
        "it cannot catch anything, and a passing self-test means nothing."
    )
    # The reason must be operator-legible about WHAT was observed: a write that
    # should have been refused went through.
    assert "no deny fired" in reason, (
        f"Expected the reason to name the observation (a write outside the grant "
        f"succeeded); got: {reason!r}"
    )


def test_probe_reports_the_escape_path_it_actually_observed():
    """Tier 2b: the failure reason names the write that got through, so an
    operator can tell "enforcement is dead" from "the probe broke" without
    reading our source."""
    reason = probe_enforcement(NoopBackend())

    assert reason is not None
    assert "escape" in reason, (
        f"Expected the reason to identify the path that was wrongly written; "
        f"got: {reason!r}"
    )


# ─── 2. The other side: a real, working backend passes ────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_seatbelt_fires_a_real_deny_on_macos():
    """Tier 2b: the probe is not a machine that always says "broken" — on a host
    with a working sandbox-exec, SeatbeltBackend fires the deny and passes.

    Pairs with the NoopBackend witness above: together they show the probe
    discriminates, rather than being stuck on one answer. (Landlock's equivalent
    is witnessed by whichever host runs Linux with the `sandbox-linux` extra —
    this one cannot speak for it.)
    """
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not present on this macOS host")

    assert probe_enforcement(backend) is None, (
        "SeatbeltBackend failed its enforcement self-test on a host where "
        "sandbox-exec is present — either the SBPL profile no longer denies "
        "writes outside write_paths, or the probe is broken."
    )


def test_noop_is_exempt_from_its_own_self_test():
    """Tier 2b: NoopBackend.self_test() returns None (exempt) even though the
    probe demonstrably fails against it.

    Not a contradiction — the two questions differ. The probe asks "does this
    enforce?" (no). `self_test()` asks "does this backend claim an enforcement it
    fails to deliver?" (also no — Noop claims none, and warns on first use). The
    exemption is structural: Noop is the fallback TARGET for every failed
    self-test, so a failing Noop would have to fall back to itself, forever.
    """
    assert NoopBackend().self_test() is None
    # …while the probe itself is not fooled — the exemption is a decision about
    # Noop's contract, not a gap in the mechanism.
    assert probe_enforcement(NoopBackend()) is not None


# ─── 3. The wiring: a failed self-test reaches on_unsupported ─────────────────


class _PresentButDead:
    """A backend whose mechanism is PRESENT but which never denies — the #2980 /
    #2962 shape, where `available()` is True and enforcement is dead.

    A Fake (a real object honouring the real Protocol), not a mock: the point is
    that the resolver's own logic runs unmodified against it.
    """

    name = "landlock"

    def available(self) -> bool:
        return True

    def self_test(self) -> str | None:
        return "no deny fired: the probe's write outside write_paths succeeded"

    def wrap_command(self, argv, policy):  # pragma: no cover — never reached
        raise AssertionError("resolution must reject this backend before use")


def test_failed_self_test_falls_back_to_noop_with_warn(monkeypatch, caplog):
    """Tier 2b: a present-but-non-enforcing backend takes the SAME fallback as an
    absent one — on_unsupported='warn' → NoopBackend + a loud WARN.

    This is the whole #2983 stage-1 claim: before this, such a backend was
    selected and used, silently, and claimed to be sandboxing.
    """
    from reyn.security.sandbox import _auto_select

    monkeypatch.setattr("platform.system", lambda: "Linux")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox"):
        result = _auto_select(None, _PresentButDead, "warn")

    assert result.name == "noop", (
        f"A backend that failed its enforcement self-test was still selected "
        f"({result.name!r}) — it would run AI code unsandboxed while reporting "
        f"that it was enforcing."
    )
    assert any("UNSANDBOXED" in r.message for r in caplog.records), (
        f"Expected a loud selection-time WARN; got: {[r.message for r in caplog.records]}"
    )


def test_failed_self_test_is_fail_closed_under_error(monkeypatch):
    """Tier 2b: on_unsupported='error' RAISES for a present-but-dead backend.

    The operator's fail-closed knob now fires on the failure mode that actually
    occurred three times in one night. Previously `error` was satisfied by a
    backend that merely imported.
    """
    from reyn.security.sandbox import _auto_select

    monkeypatch.setattr("platform.system", lambda: "Linux")

    with pytest.raises(RuntimeError, match="No OS sandbox backend available"):
        _auto_select(None, _PresentButDead, "error")


def test_failed_self_test_explains_itself_to_the_operator(monkeypatch):
    """Tier 2b: the fail-closed error carries the probe's reason, so an operator
    can act on it. "Not available" alone would be a riddle on a host where the
    package is plainly installed."""
    from reyn.security.sandbox import _auto_select

    monkeypatch.setattr("platform.system", lambda: "Linux")

    with pytest.raises(RuntimeError) as exc_info:
        _auto_select(None, _PresentButDead, "error")

    msg = str(exc_info.value)
    assert "did NOT enforce" in msg
    assert "no deny fired" in msg, (
        f"The probe's own reason must survive into the operator-facing error; "
        f"got: {msg!r}"
    )


def test_explicit_backend_self_test_failure_is_fail_closed(monkeypatch):
    """Tier 2b: the explicit path (`backend: landlock`) applies the self-test too
    — the auto path is not the only way in."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "reyn.security.sandbox.backends.landlock.LandlockBackend",
        _PresentButDead,
        raising=False,
    )

    with pytest.raises(RuntimeError) as exc_info:
        get_default_backend(SandboxConfig(backend="landlock", on_unsupported="error"))

    assert "did NOT enforce" in str(exc_info.value)


# ─── 4. Cost: the probe is paid once, and only when a sandbox is resolved ─────


def test_probe_result_is_cached_per_process():
    """Tier 2b: `enforcement_self_test` spawns its subprocesses ONCE per backend
    name per process.

    `sandboxed_exec` resolves a backend on EVERY op, so an uncached probe would
    add subprocess launches to every sandboxed op — and a broken backend would
    pay it forever. The cache is what keeps the self-test a resolution-time cost
    instead of a per-op tax (#2946 cold-start scaling stays untouched).
    """
    calls: list[str] = []

    class _CountingNoop(NoopBackend):
        name = "counting-noop"

        def wrap_command(self, argv, policy):
            calls.append(argv[-1])
            return super().wrap_command(argv, policy)

    backend = _CountingNoop()

    first = enforcement_self_test(backend)
    launches_after_first = len(calls)
    second = enforcement_self_test(backend)

    assert first == second
    assert launches_after_first > 0, "the probe must actually launch something"
    assert len(calls) == launches_after_first, (
        f"enforcement_self_test re-probed on a cached backend name "
        f"({len(calls)} launches vs {launches_after_first} after the first call) "
        f"— every sandboxed op would pay for a subprocess."
    )


def test_no_probe_runs_when_no_real_backend_is_resolved(monkeypatch):
    """Tier 2b: a run that gets no OS sandbox never pays for the probe.

    The self-test is a cost of ENFORCING, not a cost of starting up. On a
    platform with no backend (and for `backend: noop`) resolution must not spawn
    anything at all.
    """
    launched: list[list[str]] = []

    def _explode(argv, *a, **kw):
        launched.append(argv)
        raise AssertionError(f"resolution spawned a probe subprocess: {argv!r}")

    monkeypatch.setattr("reyn.security.sandbox.self_test.subprocess.run", _explode)
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")

    assert get_default_backend(SandboxConfig(backend="auto")).name == "noop"
    assert get_default_backend(SandboxConfig(backend="noop")).name == "noop"
    assert launched == []


@pytest.mark.skipif(platform.system() not in ("Darwin", "Linux"), reason="no OS backend here")
def test_probe_cost_is_bounded(monkeypatch):
    """Tier 2b: the probe completes in a time an operator would accept at
    backend resolution.

    A generous ceiling, pinning the ORDER (a couple of subprocesses), not a
    measurement — the point is that a regression turning this into a
    multi-second stall at resolution fails loudly rather than being felt as "reyn
    got slow".
    """
    import time

    start = time.monotonic()
    probe_enforcement(NoopBackend())
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, (
        f"the enforcement probe took {elapsed:.2f}s at backend resolution — "
        f"expected the cost of two short subprocess launches"
    )
