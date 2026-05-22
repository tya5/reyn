"""Tier 2: FP-0041 #489 PR-B — cron message-based shape + dispatch.

PR-A foundation landed the inbox envelope ``sender`` convention +
dispatch attribution. This PR reshapes ``CronJob`` so a scheduled
fire can deliver a free-form message to a target agent's inbox
(= sender=cron:<name>) instead of running a skill directly. Legacy
skill-based jobs continue to work for backward compat.

Pins:

  1. CronJob model gains ``to`` + ``message`` fields, retains
     ``skill`` for legacy. ``is_message_based()`` returns True iff
     both message fields are populated.
  2. CronJobConfig parses both shapes; rejects entries with neither
     OR both shapes set.
  3. ``_build_cron_config`` ValueError on invalid shape (= naming
     the offending entry in the message).
  4. Loader merges ``.reyn/cron.yaml`` into the cron section (=
     #470 invariant align, dynamic registry separate from static
     reyn.yaml).
  5. ``_merge`` cron path unions ``jobs`` by name; dynamic entries
     win on collision with legacy reyn.yaml entries.
  6. ``build_default_runner`` dispatch shapes:
     - message-based job + inbox_pusher → pusher called with
       sender=cron:<name> envelope
     - skill-based job + legacy_skill_runner → legacy called
     - message-based job + no inbox_pusher → "error" + warn
     - skill-based job + no legacy_skill_runner → "error" + warn

Tier 2 because the message shape is foundational for FP-0041 Phase
1 — Slack/LINE chat-transport handlers and the future LLM-callable
``cron_register`` tool all rely on this CronJob shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── CronJob model shape ───────────────────────────────────────────────


def test_cronjob_message_based_detected():
    """Tier 2: ``CronJob.is_message_based()`` returns True only when
    BOTH ``to`` and ``message`` are populated.
    """
    from reyn.cron import CronJob

    msg = CronJob(name="x", schedule="0 9 * * *", to="agent_x", message="hi")
    assert msg.is_message_based() is True

    legacy = CronJob(name="y", schedule="0 9 * * *", skill="some_skill")
    assert legacy.is_message_based() is False

    partial1 = CronJob(name="z", schedule="0 9 * * *", to="agent_x")
    assert partial1.is_message_based() is False

    partial2 = CronJob(name="w", schedule="0 9 * * *", message="hi")
    assert partial2.is_message_based() is False


def test_cronjob_to_dict_carries_all_shape_fields():
    """Tier 2: ``to_dict`` includes ``to``, ``message``, ``skill``
    fields regardless of which shape is set. Operators inspecting
    ``reyn cron status`` JSON see the complete row.
    """
    from reyn.cron import CronJob

    msg = CronJob(name="x", schedule="0 9 * * *", to="agent_x", message="hi")
    d = msg.to_dict()
    assert d["to"] == "agent_x"
    assert d["message"] == "hi"
    assert d["skill"] is None

    legacy = CronJob(name="y", schedule="0 9 * * *", skill="some_skill")
    d = legacy.to_dict()
    assert d["to"] is None
    assert d["message"] is None
    assert d["skill"] == "some_skill"


# ── CronJobConfig parsing ─────────────────────────────────────────────


def test_build_cron_config_parses_message_based_shape():
    """Tier 2: ``_build_cron_config`` accepts the new ``to + message``
    shape (= FP-0041 PR-B). Skill field stays None on the resulting
    config entry.
    """
    from reyn.config import _build_cron_config

    raw = {
        "jobs": [
            {
                "name": "morning_news",
                "to": "news_agent",
                "message": "今日のニュース",
                "schedule": "0 9 * * *",
                "enabled": True,
            },
        ],
    }
    cfg = _build_cron_config(raw)
    assert len(cfg.jobs) == 1
    j = cfg.jobs[0]
    assert j.name == "morning_news"
    assert j.to == "news_agent"
    assert j.message == "今日のニュース"
    assert j.skill is None


def test_build_cron_config_parses_legacy_skill_shape():
    """Tier 2: legacy skill-based jobs continue to parse — backward
    compat for existing reyn.yaml configurations.
    """
    from reyn.config import _build_cron_config

    raw = {
        "jobs": [
            {
                "name": "index_hourly",
                "skill": "index_events",
                "schedule": "0 * * * *",
            },
        ],
    }
    cfg = _build_cron_config(raw)
    assert len(cfg.jobs) == 1
    j = cfg.jobs[0]
    assert j.skill == "index_events"
    assert j.to is None
    assert j.message is None


def test_build_cron_config_rejects_both_shapes():
    """Tier 2: a job that sets both ``skill`` AND ``to``/``message``
    is rejected with ValueError naming the offending entry. Each
    shape is mutually exclusive.
    """
    from reyn.config import _build_cron_config

    raw = {
        "jobs": [
            {
                "name": "ambiguous",
                "skill": "x",
                "to": "y",
                "message": "z",
                "schedule": "0 9 * * *",
            },
        ],
    }
    with pytest.raises(ValueError, match="ambiguous"):
        _build_cron_config(raw)


def test_build_cron_config_rejects_neither_shape():
    """Tier 2: a job that sets neither ``skill`` NOR ``to``/``message``
    is rejected. Without a dispatch target the scheduler has nothing
    to fire.
    """
    from reyn.config import _build_cron_config

    raw = {
        "jobs": [
            {"name": "empty", "schedule": "0 9 * * *"},
        ],
    }
    with pytest.raises(ValueError, match="empty"):
        _build_cron_config(raw)


def test_build_cron_config_rejects_partial_message_shape():
    """Tier 2: ``to`` without ``message`` (or vice versa) is treated
    as 'no shape set' — rejected. Both fields required for the
    message-based shape.
    """
    from reyn.config import _build_cron_config

    # to set, message missing
    with pytest.raises(ValueError):
        _build_cron_config({
            "jobs": [{
                "name": "partial1",
                "to": "agent_x",
                "schedule": "0 9 * * *",
            }],
        })
    # message set, to missing
    with pytest.raises(ValueError):
        _build_cron_config({
            "jobs": [{
                "name": "partial2",
                "message": "hi",
                "schedule": "0 9 * * *",
            }],
        })


# ── .reyn/cron.yaml loader ────────────────────────────────────────────


def test_load_config_reads_dynamic_cron_yaml(tmp_path, monkeypatch):
    """Tier 2 (#470 invariant align): ``load_config`` reads
    ``.reyn/cron.yaml`` and merges its ``cron.jobs`` into the merged
    config. Sister to ``.reyn/mcp.yaml`` from #470.
    """
    from reyn.config import load_config

    _write_yaml(tmp_path / "reyn.yaml", "model: standard\n")
    _write_yaml(
        tmp_path / ".reyn" / "cron.yaml",
        "cron:\n  jobs:\n"
        "    - name: dynamic_job\n      to: my_agent\n      message: hi\n"
        "      schedule: '0 9 * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    names = [j.name for j in config.cron.jobs]
    assert "dynamic_job" in names


def test_load_config_unions_legacy_and_dynamic_cron_jobs(tmp_path, monkeypatch):
    """Tier 2: a legacy job in reyn.yaml and a dynamic job in
    .reyn/cron.yaml both surface in the merged config (= union by
    name). Operator can hand-edit AND tool-register without losing
    either side.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "cron:\n  jobs:\n"
        "    - name: legacy_job\n      skill: some_skill\n"
        "      schedule: '0 * * * *'\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "cron.yaml",
        "cron:\n  jobs:\n"
        "    - name: dynamic_job\n      to: my_agent\n      message: hi\n"
        "      schedule: '0 9 * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    names = sorted(j.name for j in config.cron.jobs)
    assert names == ["dynamic_job", "legacy_job"]


def test_load_config_dynamic_cron_yaml_overrides_legacy_on_name_collision(
    tmp_path, monkeypatch,
):
    """Tier 2: when both files have a job with the same name, the
    dynamic ``.reyn/cron.yaml`` entry wins (= last write semantics).
    Protects against double-edit losing the runtime-registered job.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "cron:\n  jobs:\n"
        "    - name: shared\n      skill: legacy_skill\n"
        "      schedule: '0 * * * *'\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "cron.yaml",
        "cron:\n  jobs:\n"
        "    - name: shared\n      to: new_agent\n      message: replaced\n"
        "      schedule: '0 9 * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert len(config.cron.jobs) == 1
    j = config.cron.jobs[0]
    assert j.name == "shared"
    # Dynamic shape wins.
    assert j.to == "new_agent"
    assert j.skill is None


def test_load_config_works_without_dynamic_cron_yaml(tmp_path, monkeypatch):
    """Tier 2: a project without ``.reyn/cron.yaml`` (= the common
    case during the migration window) still loads cleanly. Legacy
    reyn.yaml cron jobs surface unchanged.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "cron:\n  jobs:\n"
        "    - name: legacy\n      skill: x\n"
        "      schedule: '0 * * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert len(config.cron.jobs) == 1
    assert config.cron.jobs[0].name == "legacy"


# ── build_default_runner dispatch ─────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_dispatches_message_based_to_inbox_pusher():
    """Tier 2: a message-based ``CronJob`` is dispatched via the
    ``inbox_pusher`` callable, NOT the legacy skill runner. Envelope
    carries ``sender="cron:<name>"`` for PR-A attribution.
    """
    from reyn.cron import CronJob
    from reyn.cron.runners import build_default_runner

    pushed: list = []

    async def _pusher(to, envelope):
        pushed.append((to, envelope))
        return "ok"

    async def _legacy(job):
        # Should not be called for message-based jobs.
        raise AssertionError("legacy runner called for message-based job")

    runner = build_default_runner(
        legacy_skill_runner=_legacy,
        inbox_pusher=_pusher,
    )
    job = CronJob(
        name="morning_news", schedule="0 9 * * *",
        to="news_agent", message="今日のニュース",
    )
    result = await runner(job)

    assert result == "ok"
    assert len(pushed) == 1
    target, envelope = pushed[0]
    assert target == "news_agent"
    assert envelope["text"] == "今日のニュース"
    assert envelope["sender"] == "cron:morning_news"


@pytest.mark.asyncio
async def test_runner_dispatches_skill_based_to_legacy_runner():
    """Tier 2: a skill-based ``CronJob`` is dispatched via the legacy
    skill runner, NOT the inbox pusher. Backward compat for FP-0009
    skill-based jobs.
    """
    from reyn.cron import CronJob
    from reyn.cron.runners import build_default_runner

    called_with: list = []

    async def _pusher(to, envelope):
        raise AssertionError("inbox_pusher called for skill-based job")

    async def _legacy(job):
        called_with.append(job)
        return "ok"

    runner = build_default_runner(
        legacy_skill_runner=_legacy,
        inbox_pusher=_pusher,
    )
    job = CronJob(name="index_hourly", schedule="0 * * * *", skill="index_events")
    result = await runner(job)

    assert result == "ok"
    assert len(called_with) == 1
    assert called_with[0].skill == "index_events"


@pytest.mark.asyncio
async def test_runner_message_based_without_pusher_returns_error():
    """Tier 2: a message-based job in a context lacking ``inbox_pusher``
    (= CLI standalone mode, no AgentRegistry) returns "error" and
    logs a warning. Operator should use ``reyn web`` for
    message-based jobs.
    """
    from reyn.cron import CronJob
    from reyn.cron.runners import build_default_runner

    async def _legacy(job):
        return "ok"

    runner = build_default_runner(
        legacy_skill_runner=_legacy,
        inbox_pusher=None,
    )
    job = CronJob(
        name="x", schedule="0 9 * * *",
        to="agent_x", message="hi",
    )
    result = await runner(job)
    assert result == "error"


@pytest.mark.asyncio
async def test_runner_skill_based_without_legacy_runner_returns_error():
    """Tier 2: a skill-based job in a context lacking
    ``legacy_skill_runner`` returns "error" and logs a warning.
    Defensive — host process should always provide one for
    legacy support.
    """
    from reyn.cron import CronJob
    from reyn.cron.runners import build_default_runner

    async def _pusher(to, envelope):
        return "ok"

    runner = build_default_runner(
        legacy_skill_runner=None,
        inbox_pusher=_pusher,
    )
    job = CronJob(name="x", schedule="0 9 * * *", skill="some_skill")
    result = await runner(job)
    assert result == "error"
