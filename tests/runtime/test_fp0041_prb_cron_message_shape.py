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
    from reyn.runtime.cron import CronJob

    msg = CronJob(name="x", schedule="0 9 * * *", to="agent_x", message="hi")
    assert msg.is_message_based() is True

    no_fields = CronJob(name="y", schedule="0 9 * * *")
    assert no_fields.is_message_based() is False

    partial1 = CronJob(name="z", schedule="0 9 * * *", to="agent_x")
    assert partial1.is_message_based() is False

    partial2 = CronJob(name="w", schedule="0 9 * * *", message="hi")
    assert partial2.is_message_based() is False


def test_cronjob_to_dict_carries_all_shape_fields():
    """Tier 2: ``to_dict`` includes ``to`` and ``message`` fields for
    message-based jobs. Operators inspecting ``reyn cron status`` JSON
    see the complete row.
    """
    from reyn.runtime.cron import CronJob

    msg = CronJob(name="x", schedule="0 9 * * *", to="agent_x", message="hi")
    d = msg.to_dict()
    assert d["to"] == "agent_x"
    assert d["message"] == "hi"

    empty = CronJob(name="y", schedule="0 9 * * *")
    d = empty.to_dict()
    assert d["to"] is None
    assert d["message"] is None


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
    assert cfg.jobs, "parsed config must contain at least one job"
    j = cfg.jobs[0]
    assert j.name == "morning_news"
    assert j.to == "news_agent"
    assert j.message == "今日のニュース"


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
    """Tier 2: ``load_config`` reads
    ``.reyn/cron.yaml`` and merges its ``cron.jobs`` into the merged
    config. Sister to ``.reyn/mcp.yaml`` from #470.
    """
    from reyn.config import load_config

    _write_yaml(tmp_path / "reyn.yaml", "model: standard\n")
    _write_yaml(
        tmp_path / ".reyn" / "config" / "cron.yaml",
        "cron:\n  jobs:\n"
        "    - name: dynamic_job\n      to: my_agent\n      message: hi\n"
        "      schedule: '0 9 * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    names = [j.name for j in config.cron.jobs]
    assert "dynamic_job" in names


def test_load_config_unions_legacy_and_dynamic_cron_jobs(tmp_path, monkeypatch):
    """Tier 2: a static job in reyn.yaml and a dynamic job in
    .reyn/cron.yaml both surface in the merged config (= union by
    name). Operator can hand-edit AND tool-register without losing
    either side.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "cron:\n  jobs:\n"
        "    - name: static_job\n      to: static_agent\n      message: run\n"
        "      schedule: '0 * * * *'\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "config" / "cron.yaml",
        "cron:\n  jobs:\n"
        "    - name: dynamic_job\n      to: my_agent\n      message: hi\n"
        "      schedule: '0 9 * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    names = sorted(j.name for j in config.cron.jobs)
    assert names == ["dynamic_job", "static_job"]


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
        "    - name: shared\n      to: old_agent\n      message: old message\n"
        "      schedule: '0 * * * *'\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "config" / "cron.yaml",
        "cron:\n  jobs:\n"
        "    - name: shared\n      to: new_agent\n      message: replaced\n"
        "      schedule: '0 9 * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert config.cron.jobs, "merged config must contain at least one job"
    j = config.cron.jobs[0]
    assert j.name == "shared"
    # Dynamic shape wins.
    assert j.to == "new_agent"
    assert j.message == "replaced"


def test_load_config_works_without_dynamic_cron_yaml(tmp_path, monkeypatch):
    """Tier 2: a project without ``.reyn/cron.yaml`` still loads cleanly.
    Static reyn.yaml cron jobs surface unchanged.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "cron:\n  jobs:\n"
        "    - name: static_only\n      to: some_agent\n      message: run\n"
        "      schedule: '0 * * * *'\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert config.cron.jobs, "config without .reyn/cron.yaml must still load static jobs"
    assert config.cron.jobs[0].name == "static_only"


# ── build_default_runner dispatch ─────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_dispatches_message_based_to_inbox_pusher():
    """Tier 2: a message-based ``CronJob`` is dispatched via the
    ``inbox_pusher`` callable, NOT the legacy skill runner. Envelope
    carries ``sender="cron:<name>"`` for PR-A attribution.
    """
    from reyn.runtime.cron import CronJob
    from reyn.runtime.cron.runners import build_default_runner

    pushed: list = []

    # FP-0043 S4b-3a: the pusher also receives ``native_id`` (= job.name) so
    # it can route to the job's own cron:<job_name> Session.
    async def _pusher(to, envelope, native_id):
        pushed.append((to, envelope, native_id))
        return "ok"

    runner = build_default_runner(
        inbox_pusher=_pusher,
    )
    job = CronJob(
        name="morning_news", schedule="0 9 * * *",
        to="news_agent", message="今日のニュース",
    )
    result = await runner(job)

    assert result == "ok"
    assert pushed, "inbox_pusher must be called at least once for message-based job"
    target, envelope, native_id = pushed[0]
    assert target == "news_agent"
    assert envelope["text"] == "今日のニュース"
    assert envelope["sender"] == "cron:morning_news"
    # S4b-3a: the routing-key native-id is the job name (→ cron:morning_news session).
    assert native_id == "morning_news"


@pytest.mark.asyncio
async def test_runner_message_based_without_pusher_returns_error():
    """Tier 2: a message-based job in a context lacking ``inbox_pusher``
    (= CLI standalone mode, no AgentRegistry) returns "error" and
    logs a warning. Operator should use ``reyn web`` for
    message-based jobs.
    """
    from reyn.runtime.cron import CronJob
    from reyn.runtime.cron.runners import build_default_runner

    runner = build_default_runner(
        inbox_pusher=None,
    )
    job = CronJob(
        name="x", schedule="0 9 * * *",
        to="agent_x", message="hi",
    )
    result = await runner(job)
    assert result == "error"


