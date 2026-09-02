"""Tier 2: #5514 §5/§8 — the ``spillability:`` hook-config key, load-time
validation only (dispatch-time behaviour — the payload threading and the
NEVER-overflow rejection — is exercised in
``test_5514_hook_spillability_dispatch.py``).

Contract (issue #5514 §5, owner ruling 2026-08-30):

- Only ``template_push``/``exec_capture`` write history at all, so
  ``spillability:`` is rejected on any other scheme (``exec``,
  ``pipeline_launch``) — a silently-ignored declaration reads as an
  applied restriction that never applied, the same eager-rejection
  posture ``fold:``/``subprocess:``/``network:`` already use here.
- ``spillability: never`` is a self-grant at an agent-writable origin
  (per-agent/per-session) — the SAME #5356 reasoning ``write_paths``/
  ``subprocess``/``network`` already use, applied to a VALUE rather than
  a key's mere presence (``first_choice``/``last_resort`` declare
  nothing an agent-writable layer couldn't already imply by omission).
- ``spillability: never`` REQUIRES ``spillability_max_chars`` (the only
  remaining lever bounding size once offload is forbidden); the reverse
  (``spillability_max_chars`` present without ``never``) is also
  rejected, since it would be a silently-ignored declaration on any
  other tier.
"""
from __future__ import annotations

import pytest

from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.runtime.chat_message import Spillability


def _entry(scheme: str = "template_push", **extra) -> dict:
    base = {"on": "turn_end"}
    if scheme == "template_push":
        base["template_push"] = {"message": "hi"}
    elif scheme == "exec":
        base["exec"] = ["/usr/bin/true"]
    elif scheme == "exec_capture":
        base["exec_capture"] = ["/usr/bin/true"]
    base.update(extra)
    return base


def test_undeclared_spillability_defaults_to_none_on_the_hookdef() -> None:
    """Tier 2: the LOADER leaves an undeclared spillability as ``None`` —
    resolution to ``Spillability.default()`` (LAST_RESORT — #5689) happens
    at the dispatch-time push site (HookDispatcher._push_resolved), not
    here (see that test file's own docstring for why: the resolution must
    reach BOTH consumer mouths from ONE site, not be baked into the
    loader)."""
    (hook,) = load_hooks([_entry()], origin="startup").all_defs()
    assert hook.spillability is None
    assert hook.spillability_max_chars is None


@pytest.mark.parametrize("value", ["first_choice", "last_resort"])
def test_first_choice_and_last_resort_load_at_any_origin_no_cap_required(
    value: str,
) -> None:
    """Tier 2: only NEVER is the self-grant / requires-a-cap axis — the
    other two members are unrestricted."""
    for origin in ("startup", "runtime", "per-agent", "per-session"):
        (hook,) = load_hooks(
            [_entry(spillability=value)], origin=origin,
        ).all_defs()
        assert hook.spillability == Spillability(value)
        assert hook.spillability_max_chars is None


def test_never_requires_spillability_max_chars() -> None:
    """Tier 2: declaring never without a size ceiling is rejected — #5514
    §5's own "declaring picks one; both is the answer": offload is the
    other lever, and never removes it."""
    with pytest.raises(HookConfigError, match="spillability_max_chars"):
        load_hooks([_entry(spillability="never")], origin="startup")


def test_never_with_max_chars_loads_at_a_non_agent_writable_origin() -> None:
    """Tier 2: the positive side of the self-grant contrast below — the
    SAME declaration is fine at startup/runtime."""
    for origin in ("startup", "runtime"):
        (hook,) = load_hooks(
            [_entry(spillability="never", spillability_max_chars=500)],
            origin=origin,
        ).all_defs()
        assert hook.spillability is Spillability.NEVER
        assert hook.spillability_max_chars == 500


@pytest.mark.parametrize("origin", ["per-agent", "per-session"])
def test_never_rejected_at_agent_writable_origin_5356(origin: str) -> None:
    """Tier 2: #5356 self-grant reasoning applied to a VALUE (never),
    not a key's mere presence — an agent can already write its own
    per-agent/per-session hooks.yaml, so declaring never there is a
    self-grant, not an operator's expressed will. The falsification
    contrast (same entry, non-agent-writable origin, loads clean) is
    the test right above this one — this test only needs the deny side
    plus the origin-naming check below."""
    with pytest.raises(HookConfigError, match=f"never.*not permitted.*{origin!r}"):
        load_hooks(
            [_entry(spillability="never", spillability_max_chars=500)],
            origin=origin,
        )


def test_spillability_max_chars_without_never_is_rejected() -> None:
    """Tier 2: the reverse pairing error — a size ceiling declared
    without never would be silently ignored by every consumer (only the
    NEVER branch reads it), so it is rejected instead."""
    with pytest.raises(HookConfigError, match="spillability_max_chars"):
        load_hooks(
            [_entry(spillability="first_choice", spillability_max_chars=500)],
            origin="startup",
        )


@pytest.mark.parametrize("scheme", ["exec", "pipeline_launch"])
def test_spillability_rejected_on_a_non_history_writing_scheme(scheme: str) -> None:
    """Tier 2: only template_push/exec_capture write history at all —
    exec's own output is discarded (never appended), and pipeline_launch
    has no push. A declaration there would be silently ignored."""
    if scheme == "pipeline_launch":
        entry = {
            "on": "turn_end",
            "pipeline_launch": {"name": "some-pipeline"},
            "spillability": "first_choice",
        }
    else:
        entry = _entry(scheme=scheme, spillability="first_choice")
    with pytest.raises(HookConfigError, match="spillability"):
        load_hooks([entry], origin="startup")


def test_unrecognised_spillability_value_is_rejected() -> None:
    """Tier 2: a typo'd value fails loud, naming the allowed vocabulary —
    not a silent fall-through to a default the operator never chose."""
    with pytest.raises(HookConfigError, match="not a recognised value"):
        load_hooks([_entry(spillability="sometimes")], origin="startup")
