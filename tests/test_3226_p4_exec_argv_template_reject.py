"""Tests for #3226 Phase 4 follow-up (architect PASS review note) — the hook
loader rejects a Jinja2 template marker (``{{``) in an ``exec``/``exec_capture``
argv entry at load time.

Closes a doc/impl gap: ``docs/deep-dives/proposals/0059-hook-event-redesign.md``
already asserted the loader rejects ``{{``-containing argv (the invariant that
``exec``/``exec_capture`` argv is STATIC config, never Jinja2-rendered — event
data reaches the process only via stdin JSON, never via argv interpolation,
which is the command-injection guard #3226 Phase 4's docs describe), but no
code enforced it — a silent footgun: an operator writing ``{{ payload.path }}``
in an argv entry would have it passed to the subprocess LITERALLY (never
rendered, never rejected) instead of getting an actionable error.
"""
from __future__ import annotations

import pytest

from reyn.hooks import HookConfigError, load_hooks


def test_exec_argv_with_template_marker_rejected() -> None:
    """Tier 1: an ``exec`` argv entry containing ``{{`` raises ``HookConfigError``
    at load time (never silently passed through as a literal argv token)."""
    with pytest.raises(HookConfigError, match=r"\{\{"):
        load_hooks([{"on": "turn_end", "exec": ["echo", "{{ payload.path }}"]}])


def test_exec_capture_argv_with_template_marker_rejected() -> None:
    """Tier 1: the same guard applies to ``exec_capture`` — the fix-class
    sibling, not just ``exec`` (both schemes share the ``_argv`` validator)."""
    with pytest.raises(HookConfigError, match=r"\{\{"):
        load_hooks([{"on": "turn_end", "exec_capture": ["scripts/decide.sh", "{{ event }}"]}])


def test_exec_argv_without_template_marker_still_accepted() -> None:
    """Tier 1: a plain argv (no ``{{``) is unaffected — the guard is additive,
    not a behavior change for every existing valid hook."""
    registry = load_hooks([{"on": "turn_end", "exec": ["scripts/cleanup.sh", "--force"]}])
    (hd,) = registry.hooks_for("turn_end")
    assert hd.exec == ("scripts/cleanup.sh", "--force")
