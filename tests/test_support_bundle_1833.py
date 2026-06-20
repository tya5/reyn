"""Tier 2: reyn support-bundle assembles a REDACTED diagnostic zip (#1833).

The bundle reuses the existing ``reyn.llm.llm._redact_secrets`` layer on every
collected line (no new redaction). The core guard is **falsification**: a seeded
secret must be stripped from the output bundle (default ON), and — proving the
redaction (not the collection) is what strips it — the same secret REMAINS when
``REYN_LLM_TRACE_REDACT=off``.

Policy: real ``support_bundle.run`` + real redaction layer + a real zip; the only
inputs faked are the on-disk trace file + env (the OS boundary). Tier line first.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands import support_bundle

# an openai-key-shaped token (matches _DEFAULT_REDACT_PATTERNS sk-[A-Za-z0-9_-]{20,})
_SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0KLMNOP"


def _args(out: Path, *, session=None, since=None) -> argparse.Namespace:
    return argparse.Namespace(session=session, since=since, output=str(out))


def _zip_text(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        return "\n".join(zf.read(n).decode("utf-8", "replace") for n in zf.namelist())


def _seed_trace(tmp_path: Path, monkeypatch, lines: list[dict]) -> Path:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace))
    monkeypatch.chdir(tmp_path)  # no .reyn here → only the trace file is bundled
    return trace


def test_seeded_secret_redacted_from_bundle(tmp_path, monkeypatch):
    """Tier 2: a seeded secret is stripped from the bundle (default-ON redaction)."""
    monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
    _seed_trace(tmp_path, monkeypatch, [{"timestamp": datetime.now(UTC).isoformat(),
                                         "api_key": _SECRET, "msg": f"key={_SECRET}"}])
    out = tmp_path / "b.zip"
    support_bundle.run(_args(out))
    blob = _zip_text(out)
    assert _SECRET not in blob, "the seeded secret MUST be redacted from the bundle"
    assert "[REDACTED:openai-key]" in blob, "redaction marker must be present"


def test_redaction_off_leaves_secret(tmp_path, monkeypatch):
    """Tier 2: (falsification) with REYN_LLM_TRACE_REDACT=off the secret REMAINS —
    proving the redaction layer (not the collection) is what strips it. If this
    passed AND the default-on test passed, redaction is the active mechanism."""
    monkeypatch.setenv("REYN_LLM_TRACE_REDACT", "off")
    _seed_trace(tmp_path, monkeypatch, [{"timestamp": datetime.now(UTC).isoformat(),
                                         "api_key": _SECRET}])
    out = tmp_path / "b.zip"
    support_bundle.run(_args(out))
    assert _SECRET in _zip_text(out), (
        "redaction OFF must leave the secret — confirms redaction (not collection) strips it"
    )


def test_since_filter_drops_old_records(tmp_path, monkeypatch):
    """Tier 2: --since drops records older than the window, keeps newer ones."""
    monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    new = datetime.now(UTC).isoformat()
    _seed_trace(tmp_path, monkeypatch, [
        {"timestamp": old, "msg": "OLDMARKER"},
        {"timestamp": new, "msg": "NEWMARKER"},
    ])
    out = tmp_path / "b.zip"
    support_bundle.run(_args(out, since="1h"))
    blob = _zip_text(out)
    assert "NEWMARKER" in blob and "OLDMARKER" not in blob


def test_bundle_collects_trace_wal_events_three_classes(tmp_path, monkeypatch):
    """Tier 2: the bundle collects all THREE artifact classes — trace + WAL
    (.reyn/state/*.jsonl, separate from events) + events (.reyn/events/)."""
    monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
    reyn = tmp_path / ".reyn"
    (reyn / "state").mkdir(parents=True)
    (reyn / "events").mkdir(parents=True)
    (reyn / "state" / "wal.jsonl").write_text(json.dumps({"wal": "WALMARK"}) + "\n")
    (reyn / "events" / "e.jsonl").write_text(json.dumps({"ev": "EVTMARK"}) + "\n")
    _seed_trace(tmp_path, monkeypatch, [{"msg": "TRACEMARK"}])  # also chdir(tmp_path)
    out = tmp_path / "b.zip"
    support_bundle.run(_args(out))
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert any(n.startswith("trace/") for n in names), "trace class missing"
    assert any(n.startswith("wal/") for n in names), "WAL class missing (.reyn/state/)"
    assert any(n.startswith("events/") for n in names), "events class missing"


def test_bundle_has_meta_with_version(tmp_path, monkeypatch):
    """Tier 2: the bundle always contains a meta.json carrying the reyn version."""
    monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
    _seed_trace(tmp_path, monkeypatch, [{"timestamp": datetime.now(UTC).isoformat(), "msg": "x"}])
    out = tmp_path / "b.zip"
    support_bundle.run(_args(out))
    with zipfile.ZipFile(out) as zf:
        assert "meta.json" in zf.namelist()
        meta = json.loads(zf.read("meta.json"))
    assert "reyn_version" in meta and "redaction" in meta


# ── _parse_since edge handling ──────────────────────────────────────────────


def test_parse_since_relative_window_parses() -> None:
    """Tier 2: a normal relative window (Nd/Nh/Nm) returns a past datetime."""
    now = datetime.now(UTC)
    result = support_bundle._parse_since("3h")
    assert result is not None
    delta = now - result
    # ~3 hours ago (allow a wide margin for execution time)
    assert timedelta(hours=2, minutes=59) <= delta <= timedelta(hours=3, minutes=1)


def test_parse_since_huge_relative_window_is_graceful() -> None:
    """Tier 2: an over-large relative window exits gracefully, not with a traceback.

    ``timedelta(days=N)`` raises OverflowError for N beyond its C-int range. The
    relative path used to leave that uncaught (the try/except wrapped only the
    ISO path) — a typo'd giant ``--since`` crashed the CLI with a traceback
    instead of the clean ``SystemExit`` every other invalid value gets.

    Falsification: without the guard around ``timedelta``, this raises
    OverflowError (not SystemExit) and the assertion below fails.
    """
    with pytest.raises(SystemExit) as exc:
        support_bundle._parse_since("99999999999999999999d")
    assert "invalid --since" in str(exc.value)


def test_parse_since_invalid_iso_is_graceful() -> None:
    """Tier 2: a non-relative, non-ISO value exits gracefully (regression guard)."""
    with pytest.raises(SystemExit) as exc:
        support_bundle._parse_since("not-a-time")
    assert "invalid --since" in str(exc.value)


def test_parse_since_none_returns_none() -> None:
    """Tier 2: no --since (None / empty) returns None (no filter)."""
    assert support_bundle._parse_since(None) is None
    assert support_bundle._parse_since("") is None
