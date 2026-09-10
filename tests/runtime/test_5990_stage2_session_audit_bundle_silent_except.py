"""Tier 2: #5990 (part of, stage 2) — `Session._build_audit_event_bundle`'s
own `except Exception: otel_exporter = None` swallowed an OTEL-attach
failure with no visible report. Now warns before falling through to
"no OTEL export this session", same convention #6038 established.

Real Session construction (`make_session`) throughout -- only the ONE
collaborator whose failure is under test (`build_otel_exporter`) is
monkeypatched to raise, on its own real module attribute (the exact seam
`session.py`'s own local import reads at call time).
"""
from __future__ import annotations

import logging

from tests._support.agent_session import make_session

_LOGGER_NAME = "reyn.runtime.session"


def test_otel_attach_failure_warns_and_session_still_starts(
    tmp_path, monkeypatch, caplog,
) -> None:
    """Tier 2: `build_otel_exporter` raising during a REAL Session's own
    construction warns AND the session still starts with no OTEL exporter
    attached, rather than failing to construct.

    Strip-falsify (verified by hand: the `logger.warning(...)` call
    removed from `_build_audit_event_bundle`'s `except Exception`): this
    test goes red — no record at all."""
    import reyn.observability.otel_exporter as otel_module

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated OTEL attach failure")

    monkeypatch.setattr(otel_module, "build_otel_exporter", _raise)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        session = make_session(
            agent_name="test-agent-5990-stage2-otel",
            workspace_base_dir=tmp_path,
        )

    assert session is not None  # construction itself must not raise
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("otel" in r.message.lower() for r in records)


def test_normal_session_construction_does_not_warn(tmp_path, caplog) -> None:
    """Tier 2: negative control -- a real Session with no OTEL endpoint
    configured (the default) never raises inside the try, so no warning
    fires."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        make_session(
            agent_name="test-agent-5990-stage2-otel-control",
            workspace_base_dir=tmp_path,
        )
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert not any("otel" in r.message.lower() for r in records)
