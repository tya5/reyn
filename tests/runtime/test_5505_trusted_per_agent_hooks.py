"""Tier 2: #5505 — the trusted per-agent hooks layer
(`.reyn/config/agents/<name>/hooks.yaml`), architect ruling on #5351
(2026-08-29, corrected).

#5356 (merged, `988ce6e52`) closed a real confused-deputy hole: an agent
could self-grant `write_paths`/`subprocess`/`network` by declaring them at
its own (agent-writable) per-agent/per-session hooks.yaml. After #5356,
operators had ZERO mechanism to grant per-agent write_paths at all —
architect's own words: "空セルは『あれば便利』ではなく『security fix が
意図的に作った穴』". This is that missing mechanism.

Settled design (not re-litigated here):
- New layer inserted between `runtime` and `per-agent` in
  `HOOK_ORIGIN_ORDER`; #5213's `disabled:` threshold (`layer="per-agent"`)
  stays correct unchanged — the new layer is less specific.
- Trusted for free by the existing `.reyn/config/` write-gate prefix — no
  new trust mechanism.
- Carries ONLY the 3 permission-bearing keys (derived from
  `HOOK_SANDBOX_SCOPE`'s own right column, never a 2nd hand-list).
- Boot-only / fail-loud (architect ruling): captured once at `__init__`,
  never re-read on hot-reload; a malformed file refuses Session
  construction, unlike every other post-startup layer (drop + warn).

No mocks: a real Session, observed via the public inbox (E-hooks push
there) and via `_state_log`/direct file writes, mirroring
`test_2073_per_agent_hooks.py`'s own established pattern for this exact
family of test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.loader import load_trusted_per_agent_hooks
from reyn.core.events.state_log import StateLog
from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.hooks.schema import HOOK_ORIGIN_ORDER, hook_origin_is_at_least_as_specific_as
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_AGENT = "trusted-agent"
_HOOK = "hooks:\n  - on: turn_end\n    template_push:\n      message: {msg}\n      wake: true\n"
_STARTUP = [{"on": "turn_end", "template_push": {"message": "startup", "wake": True}}]


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return make_session(
        agent_name=_AGENT,
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


def _write_runtime(tmp_path: Path, msg: str) -> None:
    (tmp_path / ".reyn" / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text(_HOOK.format(msg=msg), encoding="utf-8")


def _write_per_agent(tmp_path: Path, msg: str) -> Path:
    agent_dir = tmp_path / ".reyn" / "agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "hooks.yaml"
    path.write_text(_HOOK.format(msg=msg), encoding="utf-8")
    return path


def _write_trusted_per_agent(tmp_path: Path, msg: str) -> Path:
    agent_dir = tmp_path / ".reyn" / "config" / "agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "hooks.yaml"
    path.write_text(_HOOK.format(msg=msg), encoding="utf-8")
    return path


async def _drain_texts(session: Session) -> set:
    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    return texts


# ── the loader (read directly, not via the top-level IN-set) ────────────────


def test_load_trusted_per_agent_hooks_reads_the_config_scoped_path(tmp_path: Path) -> None:
    """Tier 2: reads `.reyn/config/agents/<name>/hooks.yaml`, NOT
    `.reyn/agents/<name>/hooks.yaml` — the untrusted per-agent layer's own
    path, a different directory entirely."""
    _write_trusted_per_agent(tmp_path, "trusted")
    hooks = load_trusted_per_agent_hooks(tmp_path, _AGENT)
    assert [h.get("on", h.get(True)) for h in hooks] == ["turn_end"]


def test_load_trusted_per_agent_hooks_does_not_read_the_untrusted_path(tmp_path: Path) -> None:
    """Tier 2: falsification pair for the test above — a hook written at
    the OLD (untrusted) per-agent path must not leak into this loader's
    own read, proving the two paths are genuinely distinct, not aliases."""
    _write_per_agent(tmp_path, "untrusted-only")
    assert load_trusted_per_agent_hooks(tmp_path, _AGENT) == []


def test_load_trusted_per_agent_hooks_absent_is_empty(tmp_path: Path) -> None:
    """Tier 2: an absent trusted-per-agent file (or dir) yields [] — a
    no-op layer, never an error."""
    assert load_trusted_per_agent_hooks(tmp_path, _AGENT) == []


# ── HOOK_ORIGIN_ORDER / #5213 threshold (unaffected by the new layer) ───────


def test_trusted_per_agent_sits_between_runtime_and_per_agent() -> None:
    """Tier 2: the settled insertion point — HOOK_ORIGIN_ORDER's own order,
    architect ruling."""
    assert HOOK_ORIGIN_ORDER == (
        "startup", "runtime", "trusted-per-agent", "per-agent", "per-session",
    )


def test_5213_disabled_threshold_still_protects_trusted_per_agent() -> None:
    """Tier 2: #5213's own `disabled:` layer-bypass threshold
    (`layer="per-agent"`) needed NO change when this layer was added — it
    falls out of the existing rule for free (architect ruling): a hook
    declared at trusted-per-agent is NOT disableable via the per-agent/
    per-session `disabled:` mechanism (same protected side as startup and
    runtime), while a hook declared AT per-agent or per-session still is."""
    assert hook_origin_is_at_least_as_specific_as("trusted-per-agent", "per-agent") is False
    assert hook_origin_is_at_least_as_specific_as("per-agent", "per-agent") is True
    assert hook_origin_is_at_least_as_specific_as("per-session", "per-agent") is True


# ── the self-grant restriction stays closed at the OLD 2 origins, opens at the NEW one ──


@pytest.mark.parametrize("key,value", [
    ("write_paths", ["/tmp/somewhere"]),
    ("subprocess", True),
    ("network", True),
])
def test_trusted_per_agent_origin_accepts_the_keys_5356_rejects_elsewhere(
    key: str, value,
) -> None:
    """Tier 2: #5505's own core acceptance criterion — the exact keys #5356
    made impossible to grant per-agent load CLEAN at the new
    trusted-per-agent origin, the same as they already do at startup/
    runtime (falsification pair: the SAME entry still rejected at
    per-agent/per-session, unaffected by this change)."""
    entry = {"on": "turn_end", "exec": ["/usr/bin/true"], key: value}

    registry = load_hooks([entry], origin="trusted-per-agent")
    (hook,) = registry.all_defs()
    assert getattr(hook, key) == (tuple(value) if key == "write_paths" else value)

    for agent_writable_origin in ("per-agent", "per-session"):
        with pytest.raises(HookConfigError, match=f"{key}.*not permitted"):
            load_hooks([entry], origin=agent_writable_origin)


def test_5356_rejection_message_now_names_the_trusted_per_agent_layer() -> None:
    """Tier 2: architect's own #5505 review point — the #5356 rejection
    message pointed at startup/runtime only, BOTH agent-less layers; the
    moment a real per-agent grant mechanism exists, that guidance goes
    stale. Must be updated in the SAME PR (CLAUDE.md: a doc/message
    describing a mechanism is stale the moment the mechanism changes)."""
    entry = {"on": "turn_end", "exec": ["/usr/bin/true"], "write_paths": ["/tmp/x"]}
    with pytest.raises(HookConfigError) as exc_info:
        load_hooks([entry], origin="per-agent")
    message = str(exc_info.value)
    assert "trusted-per-agent" in message
    assert ".reyn/config/agents/<name>/hooks.yaml" in message


# ── the 5-layer additive COMBINE (boot) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_combines_all_four_file_backed_layers_additively(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: at boot the dispatcher carries startup ∪ runtime ∪
    trusted-per-agent ∪ per-agent (the 5th, per-session, is exercised in
    #2073's own suite — unaffected by this change, not re-tested here).
    Dispatching turn_end fires all four (additive; observed via the
    inbox)."""
    monkeypatch.chdir(tmp_path)
    _write_runtime(tmp_path, "runtime")
    _write_trusted_per_agent(tmp_path, "trusted")
    _write_per_agent(tmp_path, "agent")
    session = _make_session(tmp_path, hooks_config=_STARTUP)
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "runtime", "trusted", "agent"}


@pytest.mark.asyncio
async def test_trusted_per_agent_hooks_carry_the_right_origin(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a hook declared at the trusted-per-agent layer is tagged
    with that ORIGIN specifically (not "unknown", not "per-agent") — the
    #5213 provenance field this whole COMBINE depends on."""
    monkeypatch.chdir(tmp_path)
    _write_trusted_per_agent(tmp_path, "trusted")
    session = _make_session(tmp_path)
    registry = session._hook_dispatcher._registry  # noqa: SLF001
    (hook,) = [h for h in registry.hooks_for("turn_end") if h.origin == "trusted-per-agent"]
    assert hook.origin == "trusted-per-agent"


# ── boot-only: the reapply seam does NOT re-read this layer ─────────────────


@pytest.mark.asyncio
async def test_reapply_does_not_reread_trusted_per_agent_layer(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #5505's own core boot-only witness — unlike runtime/
    per-agent/per-session (see #2073's own reapply-rereads test for the
    positive control on a sibling layer), REWRITING the trusted-per-agent
    file after boot and running the hooks reapply seam does NOT pick up
    the change. This is the architect-ruled trade (no live-reload for
    this layer), not an oversight — falsified against
    `test_reapply_rereads_per_agent_layer`'s own positive-control shape
    for the untrusted per-agent sibling, proving the SAME test recipe
    behaves oppositely for this layer specifically."""
    from reyn.config.loader import load_hot_reload_config

    monkeypatch.chdir(tmp_path)
    _write_trusted_per_agent(tmp_path, "trusted_v1")
    session = _make_session(tmp_path, hooks_config=_STARTUP)

    _write_trusted_per_agent(tmp_path, "trusted_v2")  # rewrite AFTER boot
    changed = await session._reapply_hooks(load_hot_reload_config(tmp_path))
    assert changed is True  # the reapply itself still runs/returns True

    await session._hook_dispatcher.dispatch("turn_end", {})
    texts = await _drain_texts(session)
    assert "trusted_v1" in texts, "the BOOT-time content must still be the one that fires"
    assert "trusted_v2" not in texts, "a post-boot rewrite must NOT be picked up (boot-only)"


# ── fail-loud: a malformed trusted-per-agent file refuses construction ──────


def test_malformed_trusted_per_agent_layer_refuses_session_construction(tmp_path: Path) -> None:
    """Tier 2: #5505's own core fail-loud witness — a shape-malformed
    trusted-per-agent hooks.yaml (the SAME "no scheme" malformed shape
    #2073's own suite uses for its untrusted-layer drop-and-warn tests)
    raises OUT of Session construction here instead — the deliberate
    OPPOSITE of the untrusted per-agent/runtime/per-session siblings
    (falsification pair: #2073's own
    test_bad_per_agent_keeps_startup_and_runtime proves the untrusted
    layer does NOT raise for the identical malformed shape)."""
    agent_dir = tmp_path / ".reyn" / "config" / "agents" / _AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "hooks.yaml").write_text("hooks:\n  - on: turn_end\n", encoding="utf-8")

    with pytest.raises(HookConfigError):
        _make_session(tmp_path, hooks_config=_STARTUP)


@pytest.mark.asyncio
async def test_a_good_trusted_per_agent_layer_alongside_bad_runtime_still_boots(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: non-regression — a GOOD trusted-per-agent layer does not
    itself prevent boot when a SIBLING untrusted layer (runtime) is
    malformed; the untrusted layer's own independent try-add resilience
    (#2073's add-on refinement) is unaffected by this new layer sitting
    between it and per-agent in the combine order."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn" / "config").mkdir(parents=True)
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text("hooks:\n  - on: turn_end\n", encoding="utf-8")
    _write_trusted_per_agent(tmp_path, "trusted")

    session = _make_session(tmp_path, hooks_config=_STARTUP)  # must NOT raise
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "trusted"}  # bad runtime dropped, good siblings kept
