"""Tier 2: #4364 PR-2b (C-2) — ``reyn doctor``'s external-event
producer/consumer pairing.

Architect's corrected design (issue #4364): a live MCP subscription is
volatile (only meaningful on a HELD connection — doctor is a separate,
one-shot process) so the check pairs PRODUCER <-> CONSUMER, not
subscription <-> consumer. Consumer side (a hook registered for a point)
is always config-readable; producer side is readable only where a static
declaration (``fs_watch:``/``cron:``) or a past audit-log record
(``mcp_resource_updated``) exists. ``webhook_received`` has neither —
named as un-checked (D-3), never silently skipped.

Real CLI invocation (mirrors ``test_4364_pr2_doctor_hook_probe.py``'s own
established capsys-driven shape) against real config files + a real
``.reyn/events`` tree — no mocks.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.interfaces.cli.commands.doctor import run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_event(events_dir: Path, filename: str, kind: str) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / filename).write_text(
        f'{{"type": "{kind}", "timestamp": "2026-08-14T00:00:00+00:00", "data": {{}}}}\n',
        encoding="utf-8",
    )


def test_no_producers_configured_reports_only_the_uncheckable_point(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: (accept-side, absence) with no fs_watch/cron/event-log
    evidence at all, the only line printed under this section is
    webhook_received's "not checked" — a point with no producer gets no
    finding (D-2/D-3: reporting "0 hooks" for a nonexistent producer
    would be noise, not signal)."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "External-event producer/consumer pairing" in out
    assert "? webhook_received: not checked" in out
    assert "file_changed" not in out
    assert "cron_fired" not in out
    assert "mcp_resource_updated" not in out


def test_file_changed_producer_with_zero_consumers_is_flagged(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: the core witness — fs_watch declares a producer, no hook
    subscribes to file_changed -> a real ✗ finding naming the gap."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + 'fs_watch:\n  paths: ["."]\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✗ file_changed: producer present" in out
    assert "0 subscribing hooks" in out
    assert "nowhere to go" in out


def test_file_changed_producer_with_a_consumer_reports_ok(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: accept-side — the same producer, but a hook IS registered
    for file_changed -> ✓, naming the consumer count."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML
        + 'fs_watch:\n  paths: ["."]\n'
        + "hooks:\n"
        + '  - "on": file_changed\n'
        + '    name: watcher-hook\n'
        + '    template_push:\n      message: "changed"\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ file_changed: producer present" in out
    assert "1 subscribing hook(s)" in out


def test_cron_fired_only_disabled_jobs_is_not_a_producer(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: a cron job with enabled: false must NOT count as a
    producer (architect's own ruling: "enabled のみ数える") — no finding,
    same as no jobs at all."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "cron:\n"
            "  jobs:\n"
            '    - name: disabled-job\n'
            '      schedule: "0 0 * * *"\n'
            '      to: someone\n'
            '      message: "hi"\n'
            "      enabled: false\n"
        ),
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "cron_fired" not in out


def test_cron_fired_enabled_job_with_zero_consumers_is_flagged(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: an enabled cron job is a real producer; no hook subscribes
    to cron_fired -> ✗."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "cron:\n"
            "  jobs:\n"
            '    - name: real-job\n'
            '      schedule: "0 0 * * *"\n'
            '      to: someone\n'
            '      message: "hi"\n'
        ),
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✗ cron_fired: producer present" in out
    assert "1 enabled cron job(s)" in out


def test_mcp_resource_updated_with_past_evidence_and_zero_consumers_is_flagged(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: the volatile-subscription case — no LIVE subscription is
    checked (doctor cannot see one, D-1/architect's own correction); a
    PAST audit-log record of mcp_resource_updated is the producer
    evidence instead. No hook subscribes -> ✗."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_event(
        tmp_path / ".reyn" / "events", "2026-08-14T000000.jsonl", "mcp_resource_updated",
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✗ mcp_resource_updated: producer present" in out
    assert "seen in the newest" in out


def test_mcp_resource_updated_with_a_consumer_reports_ok(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: accept-side — same past evidence, but a hook subscribes to
    mcp_resource_updated -> ✓."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "hooks:\n"
            '  - "on": mcp_resource_updated\n'
            '    name: mcp-watcher\n'
            '    template_push:\n'
            '      message: "updated"\n'
        ),
    )
    _write_event(
        tmp_path / ".reyn" / "events", "2026-08-14T000000.jsonl", "mcp_resource_updated",
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ mcp_resource_updated: producer present" in out


def test_consumer_declared_only_in_runtime_hooks_yaml_still_counts(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: the 3-layer combine witness — a hook declared ONLY in
    ``.reyn/config/hooks.yaml`` (the runtime IN-set layer, not reyn.yaml's
    own startup layer) must still be counted as a consumer. Proves the
    registry read is the real 3-layer combine, not the single-face
    ``config.hooks`` read C-1's own helper uses for a different purpose."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + 'fs_watch:\n  paths: ["."]\n',
    )
    _write_yaml(
        tmp_path / ".reyn" / "config" / "hooks.yaml",
        "hooks:\n"
        '  - "on": file_changed\n'
        '    name: runtime-layer-hook\n'
        '    template_push:\n'
        '      message: "changed"\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ file_changed: producer present" in out
    assert "1 subscribing hook(s)" in out


def test_consumer_declared_only_in_per_agent_hooks_yaml_still_counts(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: same 3-layer witness, the per-agent layer — a hook declared
    ONLY under ``.reyn/agents/<name>/hooks.yaml`` must still be counted."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + 'fs_watch:\n  paths: ["."]\n',
    )
    _write_yaml(
        tmp_path / ".reyn" / "agents" / "myagent" / "hooks.yaml",
        "hooks:\n"
        '  - "on": file_changed\n'
        '    name: per-agent-hook\n'
        '    template_push:\n'
        '      message: "changed"\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ file_changed: producer present" in out
    assert "1 subscribing hook(s)" in out


def test_a_malformed_per_agent_layer_does_not_hide_a_good_siblings_finding(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: per-layer resilience — a malformed per-agent hooks.yaml
    (one agent) must not crash doctor NOR hide a real zero-responder gap
    that a DIFFERENT, well-formed layer would otherwise surface. Mirrors
    ``Session._build_hook_registry``'s own "drop the bad layer, keep the
    good ones" contract."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + 'fs_watch:\n  paths: ["."]\n',
    )
    # Malformed: `on` names an unregistered point kind — HookConfigError.
    _write_yaml(
        tmp_path / ".reyn" / "agents" / "broken-agent" / "hooks.yaml",
        "hooks:\n"
        '  - "on": not_a_real_point\n'
        '    name: broken-hook\n'
        '    template_push:\n'
        '      message: "x"\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    # The malformed layer is dropped, not crashing doctor — and since no
    # OTHER layer subscribes to file_changed, the real gap still surfaces.
    assert "✗ file_changed: producer present" in out
