"""Tier 2: #5336 — ``Session._ephemeral`` was a private-NAMED attribute
that two production sites (``AgentRegistry.spawn_session_recorded``,
``PipelineExecutorDriver``'s own run-completion teardown) already wrote
from OUTSIDE ``Session`` — a genuine external seam wearing a private name,
not an internal implementation detail CLAUDE.md/#4866 protects. Architect
ruling: this is an externally-decided FACT about the session, so the fix
is naming the seam public (``Session.mark_ephemeral()``), not hiding the
write behind a same-name property (#4866's own target — a DIFFERENT
shape, #5382).

Witness (lead-coder's own instruction): "外から書けなくなった" — that a
direct private write no longer happens ANYWHERE outside ``session.py``
itself, not merely that a public method now exists (a separate, weaker
claim: a new method could sit unused alongside the old private writes).
Python has no runtime privacy enforcement, so the only real way to prove
"cannot" is structural: scan the actual source tree for the literal
pattern and assert none survive outside the one file that owns the
attribute — CLAUDE.md's own testing policy ("use the public surface or a
snapshot-style read; if neither exists, that absence is the finding")
applies here too: no public READ accessor for the flag exists (out of
this issue's own stated scope — write-side only), so this file does not
fabricate one merely to assert on the flag's value from outside; the
structural witnesses below are what the issue's own scope supports.
"""
from __future__ import annotations

import re

from tests._support.paths import REPO_ROOT

_DIRECT_WRITE_RE = re.compile(r"\._ephemeral\s*=\s*True\b")
_THIS_FILE = __file__


def test_no_file_outside_session_py_writes_ephemeral_directly() -> None:
    """Tier 2: repo-wide structural witness — grep every real ``.py`` file
    under ``src/`` and ``tests/`` (the actual tree, not a fixture string)
    for the direct-write pattern; only ``session.py`` itself (which OWNS
    the attribute, inside :meth:`Session.mark_ephemeral`) may match.

    Strip-falsify: reverting either production call site
    (``registry.py``'s or ``pipeline_executor_driver.py``'s) back to the
    direct ``._ephemeral = True`` form makes this go RED immediately —
    the exact regression this test exists to catch."""
    offenders: list[str] = []
    for root_name in ("src", "tests"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            if path.name == "session.py" or str(path) == _THIS_FILE:
                continue
            text = path.read_text(encoding="utf-8")
            if _DIRECT_WRITE_RE.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        f"found a direct `._ephemeral = True` write outside session.py — "
        f"use Session.mark_ephemeral() instead: {offenders!r}"
    )


def test_both_real_production_callers_use_the_public_seam() -> None:
    """Tier 2: the positive half of the witness above — the sibling test
    proves the OLD pattern is gone; this one proves the two REAL
    production call sites this issue names (registry.py's spawn-time
    declaration, pipeline_executor_driver.py's run-completion teardown
    poke) actually call the new public method, rather than having simply
    stopped touching the flag at all (a different, unintended fix that
    the sibling test alone could not distinguish from this one)."""
    registry_src = (REPO_ROOT / "src" / "reyn" / "runtime" / "registry.py").read_text(
        encoding="utf-8",
    )
    driver_src = (
        REPO_ROOT / "src" / "reyn" / "runtime" / "services" / "pipeline_executor_driver.py"
    ).read_text(encoding="utf-8")
    assert "spawned_session.mark_ephemeral()" in registry_src, (
        "registry.py's spawn_session_recorded must declare a fresh "
        "ephemeral spawn via the public seam"
    )
    assert "self._session.mark_ephemeral()" in driver_src, (
        "pipeline_executor_driver.py's run-completion teardown must "
        "trigger auto-vanish via the public seam"
    )


def test_mark_ephemeral_is_callable_on_a_real_session() -> None:
    """Tier 2: minimal behavioral witness — a real ``Session`` (no mocks)
    accepts the call without raising. The flag's own EFFECT (gating
    ``SpawnTracker``'s auto-vanish scheduling) is already covered by this
    repo's existing ephemeral-vanish tests
    (test_2103_A_ephemeral_auto_vanish_1953.py,
    test_4768_ephemeral_vanish_during_global_rewind.py); this test's own
    job is narrower — confirming the PUBLIC method itself is a real,
    working replacement for the direct attribute write, not that the
    downstream vanish behavior changed (it does not — same flag, new
    name for touching it)."""
    from tests._support.agent_session import make_session

    session = make_session(agent_name="ephemeral-seam-test")
    session.mark_ephemeral()  # must not raise
