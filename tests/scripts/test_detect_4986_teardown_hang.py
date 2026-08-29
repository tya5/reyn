"""Tier 1: #4997① — the #4986 teardown-hang CI-log detector.

Pins the signature co-occurrence rule measured across two real CI runs
(#4986 issue thread): `ERROR at teardown` + `_cancel_all_tasks` +
`Timeout (>120`, all three, and the explicit exclusion of
`ephemeral-vanish` as a discriminator (real counterexample: run
31913669752 has the warning without the hang).
"""
from __future__ import annotations

from scripts.detect_4986_teardown_hang import format_notification, has_4986_signature

_REAL_POSITIVE_EXCERPT = """\
_ ERROR at teardown of test_model_output_cannot_reach_slash_dispatch_and_spawns_nothing _
[gw2] linux -- Python 3.12.14 /opt/hostedtoolcache/Python/3.12.14/x64/bin/python
        yield runner
    except Exception as e:
        runner.__exit__(type(e), e, e.__traceback__)
    else:
        with warnings.catch_warnings():
>                       runner.__exit__(None, None, None)
/opt/hostedtoolcache/Python/3.12.14/x64/lib/python3.12/asyncio/runners.py:70: in close
    _cancel_all_tasks(loop)
/opt/hostedtoolcache/Python/3.12.14/x64/lib/python3.12/asyncio/runners.py:206: in _cancel_all_tasks
    loop.run_until_complete(tasks.gather(*to_cancel, return_exceptions=True))
E           Failed: Timeout (>120.0s) from pytest-timeout.
WARNING  reyn.runtime.tracked_tasks:tracked_tasks.py:280 TrackedTaskSet.aclose(caller='await_quiescent'): called reentrantly from a task ('ephemeral-vanish', disposition='await', appends_wal=True) this tracker is itself still tracking -- that task is excluded from THIS call's own drain (see aclose()'s own docstring for why). Normal for the ephemeral-vanish task's internal call; if caller=='AgentRegistry.shutdown' here, its non-reentrancy assumption has broken.
"""


def test_the_real_positive_excerpt_is_detected():
    """Tier 1: the exact 3-marker co-occurrence from a real CI run
    (#4986 thread, run 32459227086) is detected."""
    assert has_4986_signature(_REAL_POSITIVE_EXCERPT) is True


def test_ephemeral_vanish_alone_is_not_the_signature():
    """Tier 1: real counterexample (run 31913669752) — the
    ephemeral-vanish warning appears WITHOUT the teardown hang. Must NOT
    be flagged: this is the exact false-positive the issue explicitly
    warned against."""
    body = (
        "WARNING TrackedTaskSet.aclose(caller='await_quiescent'): called "
        "reentrantly from a task ('ephemeral-vanish', disposition='await', "
        "appends_wal=True) this tracker is itself still tracking\n"
        "1 passed in 4.21s\n"
    )
    assert has_4986_signature(body) is False


def test_teardown_error_alone_without_timeout_is_not_flagged():
    """Tier 1: a teardown ERROR with no hang (an ordinary teardown
    exception, resolved well under the timeout) must not be flagged —
    only the co-occurrence with the timeout+_cancel_all_tasks frame is
    the signature."""
    body = (
        "_ ERROR at teardown of test_something _\n"
        "some ordinary AttributeError raised during fixture teardown\n"
        "1 failed in 2.31s\n"
    )
    assert has_4986_signature(body) is False


def test_an_unrelated_120s_timeout_is_not_flagged():
    """Tier 1: a Timeout (>120 with no teardown ERROR and no
    _cancel_all_tasks frame — a genuinely slow test that legitimately
    exceeded the ceiling — must not be conflated with the #4986 hang."""
    body = "E           Failed: Timeout (>120.0s) from pytest-timeout.\n"
    assert has_4986_signature(body) is False


def test_cancel_all_tasks_alone_appears_in_every_teardown_and_is_not_flagged():
    """Tier 1: `_cancel_all_tasks` by itself appears in EVERY
    asyncio-runner teardown, hung or not (it's the frame that runs
    regardless) — must not be the sole trigger."""
    body = (
        "/opt/.../asyncio/runners.py:70: in close\n"
        "    _cancel_all_tasks(loop)\n"
        "1 passed in 3.02s\n"
    )
    assert has_4986_signature(body) is False


def test_two_of_three_markers_is_not_enough():
    """Tier 1: non-vacuity for the co-occurrence rule — exactly 2 of the
    3 markers, missing the third, must not fire."""
    body = "_ ERROR at teardown of test_x _\n    _cancel_all_tasks(loop)\n"
    assert has_4986_signature(body) is False


def test_notification_names_the_issue_and_the_evidence_preservation_step():
    """Tier 1: #4997's common requirement across all 3 detectors — the
    notification must carry the issue number to trace to AND the
    evidence-preservation step, in the SAME message (lead-coder's own
    incident, same night: a notification without this step gets the
    evidence destroyed by a re-run before anyone reads it)."""
    text = format_notification("32459227086")
    assert "#4986" in text
    assert "32459227086" in text
    assert "attempts/1/logs" in text
    assert "before re-running" in text.lower()


def test_notification_points_at_the_variant_b_pending_task_line():
    """Tier 1: #4986 variant B — the notification must tell whoever picks
    up a real detection to check attempt 1's log for the "waiting on N
    tracked task(s)" line ``TrackedTaskSet.aclose()`` now emits (this
    fix's own module) — the one thing the faulthandler-based
    pytest-timeout dump cannot name (an asyncio Task, not an OS thread).
    Architect ruling: "CI の実物で出ること" is deliberately NOT the
    acceptance bar for the fix itself; THIS notification hook is instead
    what makes the next real occurrence self-identify whether the line
    was present."""
    text = format_notification("32459227086")
    assert "waiting on" in text
    assert "tracked task" in text
