"""`reyn support-bundle` — assemble a redacted diagnostic bundle (#1833).

Collects the observability artifacts Reyn already writes — the LLM payload trace
(``$REYN_LLM_TRACE_DUMP`` JSONL), the WAL + event logs under ``.reyn/events/`` —
filters them by session / time window, runs **every** collected line through the
EXISTING secret-redaction layer (``reyn.llm.llm._redact_secrets``; reused, not
reinvented — default ON, disabled only by ``REYN_LLM_TRACE_REDACT=off``), and packs
the redacted files plus a secrets-free ``meta.json`` into a single zip for support.

The parts already exist; this is the missing assembly + redaction-at-the-exit. No
new redaction logic and no provider calls.

Usage::

    reyn support-bundle [--session <id>] [--since <iso|Nd|Nh|Nm>] -o bundle.zip
"""
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser(
        "support-bundle",
        help="Assemble a redacted diagnostic bundle (trace + WAL + events) as a zip",
    )
    p.add_argument(
        "--session",
        default=None,
        help="Only include records whose session / run_id / agent_id matches this id.",
    )
    p.add_argument(
        "--since",
        default=None,
        help="Only include records at/after this time (ISO-8601, or relative Nd/Nh/Nm).",
    )
    p.add_argument(
        "-o", "--output",
        default="support-bundle.zip",
        help="Output zip path (default: support-bundle.zip).",
    )
    p.set_defaults(func=run)


def _parse_since(raw: str | None) -> datetime | None:
    """Parse --since as ISO-8601 or a relative ``Nd`` / ``Nh`` / ``Nm`` window."""
    if not raw:
        return None
    raw = raw.strip()
    if raw and raw[-1] in ("d", "h", "m") and raw[:-1].isdigit():
        n = int(raw[:-1])
        unit = {"d": "days", "h": "hours", "m": "minutes"}[raw[-1]]
        return datetime.now(UTC) - timedelta(**{unit: n})
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit(f"support-bundle: invalid --since {raw!r}: {exc}") from exc


_TS_FIELDS = ("timestamp", "ts", "time", "sent_at_iso", "created_at")
_SESSION_FIELDS = ("session", "session_id", "run_id", "agent_id", "agent", "chain_id")


def _rec_time(rec: dict) -> datetime | None:
    for f in _TS_FIELDS:
        v = rec.get(f)
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _line_included(rec: dict, since: datetime | None, session: str | None) -> bool:
    """Best-effort filter — a line is INCLUDED unless it clearly fails a set filter
    (favour completeness for diagnostics; redaction handles safety regardless)."""
    if since is not None:
        t = _rec_time(rec)
        if t is not None and t < since:
            return False
    if session is not None:
        vals = {str(rec.get(f)) for f in _SESSION_FIELDS if rec.get(f) is not None}
        if vals and session not in vals:
            return False
    return True


def _redact_text(line: str) -> str:
    """Redact one JSONL line through the EXISTING ``_redact_secrets`` layer. Parsed
    JSON → recursive dict redaction; a non-JSON line → wrap so the same string
    masking applies. No new redaction logic (#1833 guard)."""
    from reyn.llm.llm import _redact_secrets
    stripped = line.rstrip("\n")
    if not stripped:
        return line
    try:
        rec = json.loads(stripped)
    except json.JSONDecodeError:
        return _redact_secrets({"_raw": stripped})["_raw"] + "\n"
    if isinstance(rec, dict):
        return json.dumps(_redact_secrets(rec), ensure_ascii=False) + "\n"
    # JSON but not an object (list/scalar) → mask via the wrap path.
    return _redact_secrets({"_raw": stripped})["_raw"] + "\n"


def _collect_files(reyn_dir: Path | None) -> list[tuple[str, Path]]:
    """(arcname, source path) for the THREE artifact classes (#1833): the LLM
    trace, the WAL (StateLog ``.reyn/state/*.jsonl``), and the event logs
    (``.reyn/events/**/*.jsonl``). The WAL lives under ``.reyn/state/``, NOT
    ``.reyn/events/`` — collect it distinctly so the bundle is complete."""
    import os
    out: list[tuple[str, Path]] = []
    trace = os.environ.get("REYN_LLM_TRACE_DUMP")
    if trace:
        tp = Path(trace)
        if tp.is_file():
            out.append((f"trace/{tp.name}", tp))
    if reyn_dir is not None:
        # WAL — StateLog crash-recovery log at .reyn/state/wal.jsonl (PR21).
        state_dir = reyn_dir / "state"
        if state_dir.is_dir():
            for wp in sorted(state_dir.rglob("*.jsonl")):
                out.append((f"wal/{wp.relative_to(state_dir)}", wp))
        # events — the P6 audit logs.
        events_dir = reyn_dir / "events"
        if events_dir.is_dir():
            for jp in sorted(events_dir.rglob("*.jsonl")):
                out.append((f"events/{jp.relative_to(events_dir)}", jp))
    return out


def _redacted_jsonl(src: Path, since, session) -> str:
    """Read a JSONL file, drop filtered lines, redact the rest. Returns the bundled text."""
    kept: list[str] = []
    try:
        with open(src, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(_redact_text(line))  # unparseable → redact, keep
                    continue
                if isinstance(rec, dict) and not _line_included(rec, since, session):
                    continue
                kept.append(_redact_text(line))
    except OSError as exc:
        return f'{{"_bundle_error": "could not read source: {exc}"}}\n'
    return "".join(kept)


def _meta(session, since_raw, manifest) -> dict:
    from reyn.llm.llm import _redact_secrets
    try:
        from importlib.metadata import version as _v
        reyn_version = _v("reyn")
    except Exception:
        reyn_version = "unknown"
    config_summary: dict = {}
    try:
        from reyn.config import load_config
        cfg = load_config()
        config_summary = {
            "model": getattr(cfg, "model", None),
            "models": list(getattr(cfg, "models", {}) or {}),
            "api_base_set": bool(getattr(cfg, "api_base", "")),
        }
    except Exception as exc:  # never let config-load break the bundle
        config_summary = {"_unavailable": str(exc)}
    return _redact_secrets({
        "reyn_version": reyn_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "filters": {"session": session, "since": since_raw},
        "config_summary": config_summary,
        "files": manifest,
        "redaction": (
            "applied via reyn.llm.llm._redact_secrets (default ON; "
            "REYN_LLM_TRACE_REDACT=off disables — do not share an unredacted bundle)"
        ),
    })


def run(args: argparse.Namespace) -> None:
    from reyn.core.events.events import _find_reyn_dir
    since = _parse_since(args.since)
    reyn_dir = _find_reyn_dir(Path.cwd())
    files = _collect_files(reyn_dir)
    out_path = Path(args.output)

    manifest: list[dict] = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, src in files:
            text = _redacted_jsonl(src, since, args.session)
            zf.writestr(arcname, text)
            manifest.append({"file": arcname, "lines": text.count("\n")})
        meta = _meta(args.session, args.since, manifest)
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    n = len(manifest)
    print(f"support-bundle: wrote {out_path} ({n} artifact file{'s' if n != 1 else ''} + meta.json)")
    if n == 0:
        print(
            "  note: no trace/event artifacts found "
            "(set REYN_LLM_TRACE_DUMP and/or run from a project with .reyn/events/)."
        )
