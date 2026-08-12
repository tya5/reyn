"""Tier 2: startup timing reports on screen, is off by default, and admits gaps.

An operator reported `reyn chat` taking minutes on Windows / git-bash, and reyn
could not say where the time went — 93% of startup happens before the first
audit event exists. They also cannot copy files off that machine, so the report
has to be readable in the terminal rather than written somewhere.

The assertions below are about the three properties that make it usable there:
nothing is printed unless asked, every declared stage appears even at zero, and
time the stages do not explain is stated rather than hidden.
"""
from __future__ import annotations

from reyn.runtime.startup_timing import STAGES, StartupTiming, enabled, stage


def test_it_is_off_unless_the_env_var_says_otherwise(monkeypatch) -> None:
    """Tier 2: default is off.

    Startup happens on every run, so unlike the #3539 loop tripwire there is no
    "the moment arrives unannounced" case for paying anything by default.
    """
    monkeypatch.delenv("REYN_STARTUP_TIMING", raising=False)

    assert enabled() is False


def test_the_flag_accepts_what_an_operator_would_type(monkeypatch) -> None:
    """Tier 2: `1`, `true`, `on` all work; a stray value does not enable it.

    The operator turning this on is already dealing with a slow startup and may
    be relaying instructions verbally — a flag that silently ignores `true`
    would look like the feature is broken.
    """
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("REYN_STARTUP_TIMING", value)
        assert enabled() is True, value

    for value in ("0", "false", "", "maybe"):
        monkeypatch.setenv("REYN_STARTUP_TIMING", value)
        assert enabled() is False, value


def test_a_stage_that_never_ran_is_still_reported() -> None:
    """Tier 2: every declared stage appears, at zero if it did not run.

    "This took no time" and "this is missing from the report" are different
    findings, and the second is the more interesting one. A report that omits
    what did not happen cannot tell them apart.
    """
    timing = StartupTiming()
    timing.record("config", 0.5)

    lines = timing.report_lines(wall_seconds=1.0, first_frame_reached=True)
    body = "\n".join(lines)

    for name in STAGES:
        assert name in body, f"{name} vanished from the report"


def test_a_repeated_stage_accumulates() -> None:
    """Tier 2: reading config twice shows as the total, not the last read.

    #3671's own defect list includes config being loaded more than once. A
    report that overwrote per stage would hide exactly the thing being hunted.
    """
    timing = StartupTiming()
    timing.record("config", 0.30)
    timing.record("config", 0.20)

    assert timing.total_seconds == 0.50


def test_time_the_stages_cannot_explain_is_stated() -> None:
    """Tier 2: the gap is a line, not a silence.

    The most useful thing this can say is "it is not in any of these". Without
    it, a tidy breakdown of 1% of a startup reads as an answer.
    """
    timing = StartupTiming()
    timing.record("config", 0.40)

    lines = timing.report_lines(wall_seconds=40.0, first_frame_reached=True)
    unaccounted = next(line for line in lines if "unaccounted" in line)

    assert timing.unaccounted_seconds(40.0) == 39.6
    assert "39.6" in unaccounted


def test_shares_are_of_wall_time_not_of_the_measured_sum() -> None:
    """Tier 2: a stage's percentage answers "of the startup", not "of what we measured".

    Sharing out the measured sum would print `config 100%` for a startup where
    config is 1% and something unmeasured is the rest — the exact misreading
    this issue is trying to escape.
    """
    timing = StartupTiming()
    timing.record("config", 0.40)

    config_line = next(line for line in timing.report_lines(40.0, first_frame_reached=True) if "config" in line)

    assert "1.0%" in config_line


def test_the_context_manager_records_elapsed_time() -> None:
    """Tier 2: `stage()` measures the block it wraps."""
    import time

    from reyn.runtime import startup_timing

    before = startup_timing.TIMING.total_seconds
    with stage("config"):
        time.sleep(0.02)

    assert startup_timing.TIMING.total_seconds - before >= 0.02


def test_startup_ends_at_the_first_frame_not_at_process_exit(monkeypatch) -> None:
    """Tier 2: the clock stops when the interface appears.

    Measured the alternative first: bracketing the chat coroutine reported
    ``first-frame 98.5%`` on a 3.56 s "startup" — it was counting how long
    someone sat in the chat. True, and useless. Wall time is now the span from
    import to the first frame, so a long session cannot dilute the shares of
    everything that came before it.
    """
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)
    startup_timing.mark_first_frame()
    at_first_frame = startup_timing.process_elapsed_seconds()
    time.sleep(0.05)

    assert startup_timing.process_elapsed_seconds() == at_first_frame


def test_a_second_mark_does_not_move_the_end_of_startup(monkeypatch) -> None:
    """Tier 2: only the first frame counts.

    A surface that re-mounts — a session switch, a rebuild on resize — would
    otherwise push the end of startup later and shrink every share recorded
    before it, turning a stable report into one that drifts with usage.
    """
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)
    startup_timing.mark_first_frame()
    first = startup_timing.process_elapsed_seconds()
    time.sleep(0.05)
    startup_timing.mark_first_frame()

    assert startup_timing.process_elapsed_seconds() == first


def test_a_startup_that_never_reached_a_frame_still_reports(monkeypatch) -> None:
    """Tier 2: a failed or interrupted startup still has a wall time.

    This is the case the report matters most in — an operator who never got an
    interface needs to know how long they waited and where it went.
    """
    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)

    assert startup_timing.process_elapsed_seconds() > 0


def test_the_report_survives_an_interrupt(monkeypatch, capsys) -> None:
    """Tier 2: Ctrl-C during startup still prints the breakdown.

    This is the case the whole feature exists for. Someone whose startup takes
    minutes presses Ctrl-C, and an unguarded report would withhold the numbers
    from exactly that person, in exactly that situation — the "a tool is
    unreachable at the moment it was built for" shape this repo keeps finding.

    Measured while fixing it: guarding only the final `run_async` call was not
    enough. An interrupt during registry construction, several steps earlier,
    still printed nothing — whatever stage is slow IS the stage the interrupt
    lands in. Hence the guard wraps the whole of `run`.
    """
    import argparse

    from reyn.interfaces.cli.commands import chat as chat_cmd

    monkeypatch.setenv("REYN_STARTUP_TIMING", "1")

    def _interrupted(_args: argparse.Namespace) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_cmd, "_run", _interrupted)

    try:
        chat_cmd.run(argparse.Namespace())
    except KeyboardInterrupt:
        pass

    assert "startup timing" in capsys.readouterr().out


def test_client_prep_is_four_named_sub_stages_not_one_lump(monkeypatch) -> None:
    """Tier 2: #3671 (architect's design) — the single `client-prep` lump is
    replaced by named sub-stages at the seams already in the code (transport
    construction, read-model construction, the lazy textual/flowview import,
    and app construction), so a slow startup can say WHICH of these, not
    just that the client was slow to get ready.

    `litellm-import` briefly existed as a 5th sub-stage (#3671 follow-up)
    bracketing a startup-time `import litellm` that `run_async` used to
    force unconditionally — REMOVED, not merely un-asserted here, once that
    forced import itself was removed (`reyn.llm.litellm_bootstrap` module
    docstring): a session that never calls the LLM no longer pays a
    litellm-import cost during startup at all, so there is nothing left for
    a 5th bracket to name."""
    assert "client-prep" not in STAGES
    assert "client-prep:litellm-import" not in STAGES
    for name in (
        "client-prep:transport",
        "client-prep:read-model",
        "client-prep:tui-import",
        "client-prep:app-construct",
    ):
        assert name in STAGES


def test_tui_import_done_then_app_constructed_records_app_construct_stage(
    monkeypatch,
) -> None:
    """Tier 2: P4 — the span between the lazy textual_chat import finishing
    and the TUI object existing is recorded as `client-prep:app-construct`,
    the same paired-mark shape `mark_app_constructed`/`mark_first_frame`
    already use for a region a `with` block cannot hold (it crosses an
    `await` into a different module)."""
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_TUI_IMPORT_DONE_AT", None)
    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    before = startup_timing.TIMING.total_seconds

    startup_timing.mark_tui_import_done()
    time.sleep(0.02)
    startup_timing.mark_app_constructed()

    after = startup_timing.TIMING.total_seconds
    assert after - before >= 0.02


def test_app_constructed_without_a_prior_tui_import_mark_records_nothing(
    monkeypatch,
) -> None:
    """Tier 2: FALSIFY — if `mark_tui_import_done` was never called (e.g. a
    future non-textual renderer reaches `mark_app_constructed` some other
    way), no `client-prep:app-construct` time is recorded rather than
    computing a bogus span against a stale/absent mark. Mirrors the existing
    `if _ASYNC_ENTERED_AT is not None` guard shape."""
    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_TUI_IMPORT_DONE_AT", None)
    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    before = startup_timing.TIMING.total_seconds

    startup_timing.mark_app_constructed()

    assert startup_timing.TIMING.total_seconds == before


def test_a_second_tui_import_mark_does_not_move_the_span(monkeypatch) -> None:
    """Tier 2: idempotent, mirroring `mark_first_frame`'s own "only the first
    call counts" contract — a re-import (should never happen, but the mark
    is defensive the same way the others are) must not shrink P4 by resetting
    the start point. Witnessed through the PUBLIC recorded duration (not the
    private timestamp): if the second mark moved the start forward, the
    recorded span would only cover the second sleep, not both.
    """
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_TUI_IMPORT_DONE_AT", None)
    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    before = startup_timing.TIMING.total_seconds

    startup_timing.mark_tui_import_done()
    time.sleep(0.02)
    startup_timing.mark_tui_import_done()  # no-op: only the first call counts
    time.sleep(0.02)
    startup_timing.mark_app_constructed()

    after = startup_timing.TIMING.total_seconds
    assert after - before >= 0.04


def test_gap_between_named_substages_is_captured_not_lost_falsify(monkeypatch) -> None:
    """Tier 2: FALSIFY — #3735 regression. The control-flow BETWEEN the 4
    named `client-prep:*` sub-stages (registry/session setup before
    `:transport` starts, `resolve_render_mode` + branch dispatch between
    `:read-model` and `:tui-import`, …) is not covered by any `stage()`/mark
    pair. Before the fix this fell silently out of `client-prep` entirely and
    into the process-wide `unaccounted` bucket — the coverage hole the
    owner's real-machine re-measurement caught (unaccounted 6.9% -> 62%,
    traced to the 4 sub-stages replacing the old wide bracket instead of
    breaking it down). Witnessed by injecting real sleeps in that gap and
    confirming they land in `client-prep:other`, not vanishing — "fired" is
    not "covers"."""
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_ASYNC_ENTERED_AT", None)
    monkeypatch.setattr(startup_timing, "_TUI_IMPORT_DONE_AT", None)
    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    # A fresh instance, not the shared process-wide TIMING: the `named` sum
    # `mark_app_constructed` computes is a one-shot lifetime total (correct
    # for real usage, where each mark/stage fires once per process) — reusing
    # the module singleton across test functions would pollute it with every
    # prior test's own recordings of the same stage names.
    monkeypatch.setattr(startup_timing, "TIMING", StartupTiming())

    startup_timing.mark_async_entered()
    time.sleep(0.02)  # gap 1 — untracked control flow before `:transport`
    with stage("client-prep:transport"):
        time.sleep(0.01)
    with stage("client-prep:read-model"):
        time.sleep(0.01)
    time.sleep(0.02)  # gap 2 — untracked control flow before `:tui-import`
    with stage("client-prep:tui-import"):
        time.sleep(0.01)
    startup_timing.mark_tui_import_done()
    startup_timing.mark_app_constructed()

    other = startup_timing.TIMING.elapsed("client-prep:other")
    assert other >= 0.03, f"the two injected gaps (0.04s) were not captured: {other}s"


def test_named_substages_plus_other_equal_the_wide_span(monkeypatch) -> None:
    """Tier 2: coverage invariant — #3671's original single `client-prep` lump
    (`mark_app_constructed` minus `mark_async_entered`) must still be fully
    explained after being broken into 4 named sub-stages + `client-prep:other`
    (#3735 fix): their sum must equal the wide span, not merely be LESS than
    or equal to it. Measured against the test's OWN wall clock (not the
    module's private timestamps) so this is a public-surface witness, not an
    assertion on internal state."""
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_ASYNC_ENTERED_AT", None)
    monkeypatch.setattr(startup_timing, "_TUI_IMPORT_DONE_AT", None)
    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    monkeypatch.setattr(startup_timing, "TIMING", StartupTiming())
    stages = (
        "client-prep:transport", "client-prep:read-model",
        "client-prep:tui-import", "client-prep:app-construct", "client-prep:other",
    )

    wall_start = time.perf_counter()
    startup_timing.mark_async_entered()
    time.sleep(0.01)
    with stage("client-prep:transport"):
        time.sleep(0.01)
    time.sleep(0.01)
    with stage("client-prep:read-model"):
        time.sleep(0.01)
    with stage("client-prep:tui-import"):
        time.sleep(0.01)
    startup_timing.mark_tui_import_done()
    time.sleep(0.01)
    startup_timing.mark_app_constructed()
    wall_end = time.perf_counter()

    named_sum = sum(startup_timing.TIMING.elapsed(name) for name in stages)
    assert abs(named_sum - (wall_end - wall_start)) < 0.02


def test_tui_boot_named_substages_plus_other_equal_the_wide_span(monkeypatch) -> None:
    """Tier 2: #3671 follow-up — the SAME coverage invariant as
    ``test_named_substages_plus_other_equal_the_wide_span`` above, applied
    to ``tui-boot``'s own breakdown (owner git-bash re-measurement:
    ``tui-boot`` was a single unbroken 23.9s span). The 3 named sub-stages
    (``tui-boot:construct``/``:compose``/``:hydrate``) plus
    ``tui-boot:other`` must sum to the wide ``mark_app_constructed`` ->
    ``mark_first_frame`` span exactly, not merely be less than or equal to
    it — the #3735 regression shape this module's whole ``:other`` pattern
    exists to prevent."""
    import time

    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_ASYNC_ENTERED_AT", None)
    monkeypatch.setattr(startup_timing, "_TUI_IMPORT_DONE_AT", None)
    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)
    monkeypatch.setattr(startup_timing, "TIMING", StartupTiming())
    stages = ("tui-boot:construct", "tui-boot:compose", "tui-boot:hydrate", "tui-boot:other")

    wall_start = time.perf_counter()
    startup_timing.mark_app_constructed()
    with stage("tui-boot:construct"):
        time.sleep(0.01)
    time.sleep(0.01)  # the gap a narrow-bracket-only regression would drop
    with stage("tui-boot:compose"):
        time.sleep(0.01)
    with stage("tui-boot:hydrate"):
        time.sleep(0.01)
    startup_timing.mark_first_frame()
    wall_end = time.perf_counter()

    named_sum = sum(startup_timing.TIMING.elapsed(name) for name in stages)
    assert abs(named_sum - (wall_end - wall_start)) < 0.02


def test_total_seconds_does_not_double_count_tui_boot(monkeypatch) -> None:
    """Tier 2: #3671 follow-up — ``tui-boot`` (the wide bracket) and its own
    ``tui-boot:construct``/``:compose``/``:hydrate``/``:other`` breakdown
    measure the SAME interval. ``total_seconds`` (and therefore
    ``unaccounted_seconds``, which subtracts it from wall time) must count
    that interval once, not twice — falsification: summing both naively
    would put ``total_seconds`` at roughly double the real span, clamping
    ``unaccounted_seconds`` to 0 even when a genuine coverage gap exists
    elsewhere in the startup."""
    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_APP_CONSTRUCTED_AT", None)
    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)
    timing = StartupTiming()
    monkeypatch.setattr(startup_timing, "TIMING", timing)

    startup_timing.mark_app_constructed()
    with stage("tui-boot:construct"):
        pass
    with stage("tui-boot:compose"):
        pass
    with stage("tui-boot:hydrate"):
        pass
    startup_timing.mark_first_frame()

    wide = timing.elapsed("tui-boot")
    assert wide > 0.0, "tui-boot must have actually recorded something to test against"
    # total_seconds must be close to the wide span alone, not ~2x it.
    assert timing.total_seconds < wide * 1.5


def test_unaccounted_warning_line_appears_when_coverage_is_bad() -> None:
    """Tier 2: #3735 — the report self-flags a large `unaccounted` share
    instead of leaving it to blend in with the other rows. This is the "gate"
    that would have caught #3735 the moment anyone ran the report: a
    coverage hole reads as a visible warning, not a quiet number."""
    timing = StartupTiming()
    timing.record("config", 0.1)

    lines = timing.report_lines(wall_seconds=1.0, first_frame_reached=True)  # 90% unaccounted

    assert any("coverage gap" in line for line in lines)


def test_no_warning_line_when_coverage_is_good() -> None:
    """Tier 2: the warning is conditional, not always-on — a startup whose
    stages explain most of the wall time must not carry a false-alarm line."""
    timing = StartupTiming()
    timing.record("config", 0.95)

    lines = timing.report_lines(wall_seconds=1.0, first_frame_reached=True)  # 5% unaccounted

    assert not any("coverage gap" in line for line in lines)


def test_client_prep_other_is_declared() -> None:
    """Tier 2: `client-prep:other` (#3735) is a declared stage like its 4
    named siblings — so a startup where it never fires (e.g. the non-TUI
    `--cui` path, where `mark_app_constructed` is never called) still shows
    `0.00` rather than disappearing."""
    assert "client-prep:other" in STAGES


# #3671 P5: test_litellm_import_stage_is_bracketed_in_the_chat_startup_path
# retired (clean break, CLAUDE.md testing.md § extracted-refactor test
# lifecycle). It pinned `chat.py`'s `_prepay_litellm_import` bracketing a
# startup-time `import litellm` that has since been removed entirely, not
# relabelled — `run_async`/`litellm_bootstrap` no longer force that import
# for an LLM-free session, so there is no bracket left to witness. See
# `test_run_async_never_imports_litellm_without_an_llm_call`
# (test_llm_run_async_client_cache_scope_3434.py) for what replaced it.


def test_first_frame_reached_is_false_until_the_mark_fires(monkeypatch) -> None:
    """Tier 2: #3671 follow-up — the report needs a way to ask "did the
    interface actually appear" independent of the numeric elapsed time, since
    `process_elapsed_seconds()` returns a plausible-looking number either way
    (see its own docstring: measured fooling 3 real measurements in a row)."""
    from reyn.runtime import startup_timing

    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)
    assert startup_timing.first_frame_reached() is False

    startup_timing.mark_first_frame()
    assert startup_timing.first_frame_reached() is True


def test_report_says_the_interface_never_appeared_instead_of_a_total() -> None:
    """Tier 2: #3671 follow-up — FALSIFY the exact failure this session's own
    measurement hit 3 times in a row: a `reyn chat` interrupted before the
    interface existed printed a `TOTAL ... (start -> interface on screen)`
    line indistinguishable from a completed startup, with every client-prep
    stage reading 0.00s and only a >=20% `unaccounted` warning as a (missed)
    hint something was wrong. `report_lines(first_frame_reached=False)` must
    say the interface never appeared, prominently, and must NOT print a
    `TOTAL` line claiming it did."""
    timing = StartupTiming()
    timing.record("import", 0.4)

    lines = timing.report_lines(wall_seconds=3.0, first_frame_reached=False)
    body = "\n".join(lines)

    assert "NEVER appeared" in body
    assert not any(line.strip().startswith("TOTAL") for line in lines), body
    assert any(line.strip().startswith("ELAPSED") for line in lines), body


def test_report_still_says_total_when_the_interface_did_appear() -> None:
    """Tier 2: the new warning is conditional — a completed startup must keep
    its ordinary `TOTAL (start -> interface on screen)` line unchanged, the
    default `first_frame_reached=True` this function always had."""
    timing = StartupTiming()
    timing.record("import", 0.4)

    lines = timing.report_lines(wall_seconds=1.0, first_frame_reached=True)
    body = "\n".join(lines)

    assert "TOTAL" in body
    assert "interface on screen" in body
    assert "never appeared" not in body


def test_the_report_call_site_passes_first_frame_reached(monkeypatch, capsys) -> None:
    """Tier 2: #3671 follow-up — `_report_startup_timing` (the real call site,
    not a re-implementation) must actually thread `first_frame_reached()`
    through to `report_lines`, or the fix above never reaches an operator's
    screen. Drives it through `chat_cmd.run` exactly like the existing
    interrupt/exception survival tests, with the interface never having
    appeared (a fresh process's `_FIRST_FRAME_AT` is `None` by default)."""
    import argparse

    from reyn.interfaces.cli.commands import chat as chat_cmd
    from reyn.runtime import startup_timing

    monkeypatch.setenv("REYN_STARTUP_TIMING", "1")
    monkeypatch.setattr(startup_timing, "_FIRST_FRAME_AT", None)

    def _interrupted(_args: argparse.Namespace) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_cmd, "_run", _interrupted)

    try:
        chat_cmd.run(argparse.Namespace())
    except KeyboardInterrupt:
        pass

    out = capsys.readouterr().out
    assert "NEVER appeared" in out
    assert not any(
        line.strip().startswith("TOTAL") for line in out.splitlines()
    ), out


def test_the_report_survives_an_exception(monkeypatch, capsys) -> None:
    """Tier 2: a startup that dies still reports.

    `finally` rather than `except KeyboardInterrupt`, because a crash is also a
    startup someone wants the numbers for — and it is the case where "how far
    did it get" is hardest to reconstruct afterwards.
    """
    import argparse

    from reyn.interfaces.cli.commands import chat as chat_cmd

    monkeypatch.setenv("REYN_STARTUP_TIMING", "1")

    def _boom(_args: argparse.Namespace) -> None:
        raise RuntimeError("startup blew up")

    monkeypatch.setattr(chat_cmd, "_run", _boom)

    try:
        chat_cmd.run(argparse.Namespace())
    except RuntimeError:
        pass

    assert "startup timing" in capsys.readouterr().out
