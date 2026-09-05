"""Tier 2: #5818 -- ``sandbox.mode: strict`` actually reaches a real,
production op context (owner-hit, security).

Owner-hit: ``sandbox.mode: strict`` validated, was implemented
(``_SANDBOX_STRICT_MODE_DEFAULTS``, ``policy.py``), and was documented --
but no production call site ever passed ``mode=`` to
``resolve_sandbox_policy``, so the resolved policy was ALWAYS ``compat``
regardless of what an operator configured (architect's own real
end-to-end measurement, #5818). Real ``Session``/``RouterHostAdapter`` --
no mocks: this test drives the SAME
``build_router_op_context``/``RouterOpContextSource`` path a real
``reyn chat`` turn uses.

Per architect's own explicit acceptance criterion: "the production call
site strip-falsifies red" -- pinned by this file's own falsify note (see
each test's own docstring) rather than merely asserting the field got
filled (filled is not a witness that it is ENFORCED --
test_sandbox_model_completion_1339.py's own established convention for
this exact class of defect).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import SandboxConfig
from reyn.core.events.state_log import StateLog
from tests._support.agent_session import make_session


def _session(tmp_path: Path, *, sandbox_config: "SandboxConfig | None"):
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        sandbox_config=sandbox_config,
    )


def test_strict_mode_op_context_has_the_real_policy(tmp_path) -> None:
    """Tier 2: a real Session constructed with ``SandboxConfig(mode="strict")``
    produces a real chat-router OpContext whose ``default_sandbox_policy``
    genuinely has network off / subprocess denied / env allow-list empty --
    not merely a filled-in placeholder.

    Falsify note (verified in-file, Edit-only, per this session's own
    established discipline): reverting ``router_op_context.py``'s own
    ``mode=sandbox_config.mode if sandbox_config is not None else "compat"``
    kwarg back to omitted turns this RED (``network`` reads ``True``, the
    compat default, instead of ``False``) -- the exact pre-#5818 defect."""
    session = _session(tmp_path, sandbox_config=SandboxConfig(mode="strict"))
    pol = session._make_router_op_context().default_sandbox_policy
    assert pol is not None
    assert pol["network"] is False
    assert pol["deny_subprocess"] is True
    assert pol["allow_env_names"] == []


def test_compat_mode_op_context_keeps_the_pre_5818_default(tmp_path) -> None:
    """Tier 2: acceptance criterion ③ -- ``compat``'s own default behavior
    does not change by a single bit. Same real session/op-context path as
    the strict-leg test above, ``mode`` omitted from ``SandboxConfig`` (its
    own dataclass default), mirroring
    ``test_chat_session_factory_resolves_concrete_policy``'s own established
    assertion in ``test_sandbox_model_completion_1339.py``."""
    from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

    session = _session(tmp_path, sandbox_config=None)
    pol = session._make_router_op_context().default_sandbox_policy
    assert pol is not None
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK
    assert "deny_subprocess" not in pol


def test_strict_mode_yields_to_an_explicit_operator_network_allow(tmp_path) -> None:
    """Tier 2: mode never decides DIRECTION -- an explicit operator
    ``sandbox.policy.network: true`` under ``mode: strict`` still wins
    (mirrors ``test_strict_mode_default_yields_to_an_explicit_operator_
    allow``'s own established witness in ``test_sandbox_factory.py``, but
    through the REAL production op-context path rather than a bare
    ``resolve_sandbox_policy`` call)."""
    session = _session(
        tmp_path,
        sandbox_config=SandboxConfig(mode="strict", policy={"network": True}),
    )
    pol = session._make_router_op_context().default_sandbox_policy
    assert pol is not None
    assert pol["network"] is True
    # the OTHER strict-mode axes the operator did NOT write still apply.
    assert pol["deny_subprocess"] is True


def test_resolve_sandbox_policy_rejects_an_omitted_mode() -> None:
    """Tier 2: acceptance criterion ② -- omitting ``mode`` is a ``TypeError``,
    not a silent ``"compat"`` fallback (this repo's own established pattern
    for "a caller silently falling back to a default caused real harm" --
    5 identical closures the same night: ``build_active_predicate``,
    ``checkout``, ``resume``, ``latest_pipeline_state``, ``_load_yaml``).
    A future call site cannot reintroduce this issue's own root cause by
    simply forgetting the keyword."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    with pytest.raises(TypeError):
        resolve_sandbox_policy(None, write_paths=["/repo"])  # type: ignore[call-arg]
