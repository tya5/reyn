"""Structural gate: local scoped-pytest runs must not silently hide skips
caused by a missing optional-dependency extra (#4104).

The failure mode (#4101, 2026-08-10): a developer sweeps ``tests/tools/`` +
``tests/runtime/`` locally, gets "2567 passed / 0 failed", and pushes. CI runs
RED, because ``tests/runtime/test_fp0063_arc_witness.py`` was never in the
local "2567" — it was collected, then silently skipped (``pytest.importorskip``
on ``builtin-rag``'s deps), and a skip wears the same green colour a pass does
in an ordinary terminal summary glance. CLAUDE.md's six-question ④ ("would it
stay green having never run") applies here one level up: not to a single
test's own mechanism, but to a developer's whole local sweep.

**What this does**: at the end of a LOCAL run (never inside CI or an xdist
worker — see the guards below), scan the terminal reporter's own skip
records for a reason that reads as an optional-dependency gap
(``pytest.importorskip(..., reason=...)``'s conventional phrasing: contains
"not installed" or an extra name in backticks/brackets — see
``_EXTRA_GAP_MARKERS``) and print a **separate, loud** summary line distinct
from pytest's own (already-printed) SKIPPED summary. The goal is not to
change what ran — a missing extra is often a legitimate, intentional
narrowing of a local venv — it is to make the fact impossible to miss
without reading it, the same discipline the six-question checklist already
applies to a single test.

**Scope, deliberately narrow** (lead-coder ruling, #4104, 2026-08-10): this
covers ONLY "a test never entered collection/execution due to a missing
extra." It does NOT cover a test that ran and silently no-op'd via an
internal fallback (#4127's own instance the same night — code never
reached, not code never run) or a test that ran, asserted, and passed for
the wrong reason (#4128/#2081's instance — a swallowed exception, not a
missing dependency). Those are different axes; mixing them into one gate
would make none of the three measurable.

**Population, measured not assumed** (e2e-coder, #4104 comment,
2026-08-10): the full set of ``pytest.importorskip`` targets in this
repo's ``tests/`` tree is small and enumerable — ``apsw``, ``chonkie``,
``fastapi``, ``fastmcp``, ``httpx``, ``huggingface_hub``, ``linebot.v3``,
``mcp.server``, ``mcp``, ``opentelemetry.sdk.trace``, ``slack_bolt``,
``trafilatura``, ``watchdog``. Some of these (``httpx`` via the core
``litellm`` dependency) are effectively always present; others
(``apsw``/``chonkie`` for ``builtin-rag``, ``linebot.v3``, ``slack_bolt``)
are genuinely optional and this is exactly the surface #4101 hit.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# Conventional phrasing pytest.importorskip's `reason=` kwarg uses across this
# repo's test suite (grep-verified, #4104) plus importorskip's own DEFAULT
# message shape ("could not import '<module>'") for call sites that don't
# pass a custom reason.
_EXTRA_GAP_MARKERS = ("not installed", "could not import")


def _skip_reason(report: "pytest.TestReport") -> "str | None":
    """Extract the skip reason pytest recorded for a SKIPPED report.

    ``longrepr`` for a ``pytest.importorskip``-triggered skip is normally a
    ``(path, lineno, reason)`` tuple; some skip sources (a bare
    ``@pytest.mark.skip``) instead give a bare string. Handle both — a
    third shape here would mean a pytest internals change, not a #4104
    regression, so this returns ``None`` rather than raising.
    """
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    if isinstance(longrepr, str):
        return longrepr
    return None


def pytest_sessionfinish(session: "pytest.Session", exitstatus: int) -> None:
    """Print a loud, separate tally of extra-dependency-gap skips.

    No CI-specific guard is needed: CI installs every extra, so ``hits``
    is always empty there and nothing prints — the same "correct by
    construction, not by env-var-branching" property ``network_gate``'s
    own guard comment argues for. Guarded off only by an explicit opt-out
    (``REYN_DISABLE_EXTRA_SKIP_REPORT``) and inside an xdist worker (each
    worker sees only ITS OWN slice of collected tests; only the
    controller process — the one running this function without
    ``session.config.workerinput`` set — sees the whole run, mirroring
    ``network_gate.pytest_sessionfinish``'s identical guard for the same
    reason).
    """
    if os.environ.get("REYN_DISABLE_EXTRA_SKIP_REPORT") == "1":
        return
    if hasattr(session.config, "workerinput"):
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return

    hits: "list[tuple[str, str]]" = []
    for report in reporter.stats.get("skipped", []):
        reason = _skip_reason(report)
        if reason is None:
            continue
        if any(marker in reason for marker in _EXTRA_GAP_MARKERS):
            hits.append((report.nodeid, reason))

    if not hits:
        return

    lines = [
        "",
        f"⚠️  {len(hits)} test(s) skipped due to a missing optional-dependency "
        "extra (#4104) — a passing local sweep can still miss these tests "
        "entirely, and CI (which installs every extra) will run them:",
    ]
    for nodeid, reason in sorted(hits):
        lines.append(f"  - {nodeid}: {reason}")
    reporter.write_line("\n".join(lines))
