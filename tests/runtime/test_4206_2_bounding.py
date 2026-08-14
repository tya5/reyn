"""Tier 1/2: #4206 ② — the bounding axis (``model`` key only).

Unlike ③'s free-override composition (last-present-wins, no ceiling), ②'s
composition is restrict-only: an agent/session layer may narrow the
effective ``model`` ceiling, never widen it. This file covers, bottom-up:

- ``reyn.runtime.bounding``'s own pure functions (Tier 1: contract).
- ``AgentProfile.bounding`` round-trip + unknown-key validation (Tier 1).
- ``Session.model_class_ceiling``'s live 3-layer composition, INCLUDING the
  mandatory falsify-side restrict-only witness lead-coder specified
  verbatim for #4206 ②: parent ceiling=standard, child declares strong ->
  the composed ceiling must still enforce rejection (child cannot widen);
  child narrows to light -> must be allowed (Tier 2).
- ``RouterHostAdapter.model_class_ceiling()`` callback wiring (Tier 2).
- an end-to-end witness: a real turn through ``RouterLoop.run``, backed by
  a real ``Session``/resolver/``recorded_acompletion``, where a widened
  child declaration is REJECTED before ``litellm.acompletion`` is ever
  invoked (Tier 2).

Real ``Session`` (``make_session``) + real ``RouterHostAdapter`` reached
through ``session.router_host``, never a stand-in — same discipline
``test_4724_slice_b_session_wiring.py`` established for ③.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import litellm
import pytest
import yaml

from reyn.llm.model_resolver import (
    ModelClassExceedsCeilingError,
    ModelResolver,
    model_class_exceeds_ceiling,
)
from reyn.runtime.bounding import (
    BOUNDING_KEYS,
    UnknownBoundingKeyError,
    compose_model_ceiling,
    validate_bounding,
)
from reyn.runtime.profile import AgentProfile
from tests._support.agent_session import make_session

# ---------------------------------------------------------------------------
# bounding.py — pure functions (Tier 1: contract)
# ---------------------------------------------------------------------------


def test_bounding_keys_is_model_only() -> None:
    """Tier 1: contract — #4206 ②'s current scope is exactly {"model"}
    (lead-coder ruling narrowing the axis from architect's proposal)."""
    assert BOUNDING_KEYS == frozenset({"model"})


def test_compose_model_ceiling_ignores_none_layers() -> None:
    """Tier 1: contract — a layer that declares no ceiling never widens
    the effective one; all-None composes to None (unbounded)."""
    assert compose_model_ceiling(None, None, None) is None
    assert compose_model_ceiling("standard", None) == "standard"
    assert compose_model_ceiling(None, "light") == "light"


def test_compose_model_ceiling_narrowest_wins_a_wider_child_cannot_widen() -> None:
    """Tier 1: THE restrict-only witness at the pure-function level — a
    child layer declaring a WIDER (more expensive) class than the parent
    does not move the composed ceiling; only a NARROWER child does."""
    # Parent (project) ceiling=standard, child declares strong (wider) —
    # composed stays standard, the parent's own ceiling.
    assert compose_model_ceiling("standard", "strong") == "standard"
    # Parent ceiling=standard, child narrows to light — composed follows
    # the child down to light.
    assert compose_model_ceiling("standard", "light") == "light"
    # Three layers, mixed order — still the narrowest of all of them.
    assert compose_model_ceiling("strong", "standard", "light") == "light"
    assert compose_model_ceiling("light", "strong", "standard") == "light"


def test_compose_model_ceiling_incomparable_value_ignored_not_raised() -> None:
    """Tier 1: contract — a ceiling value outside STANDARD_CLASSES mirrors
    ``model_class_exceeds_ceiling``'s own "not comparable" scope limit:
    ignored, not raised, and never narrows anything by itself."""
    assert compose_model_ceiling("standard", "custom-tier") == "standard"
    assert compose_model_ceiling("custom-tier", "custom-tier-2") is None


def test_validate_bounding_raises_on_unknown_key() -> None:
    """Tier 1: contract — the #4655 "Kind① loud, not silent" discipline:
    a typo'd/renamed bounding key raises, not silently does nothing."""
    with pytest.raises(UnknownBoundingKeyError):
        validate_bounding({"timeout": "30s"}, source="test")


def test_validate_bounding_accepts_known_key() -> None:
    """Tier 1: accept-side — a recognized key does not raise."""
    validate_bounding({"model": "light"}, source="test")


# ---------------------------------------------------------------------------
# THE mandatory falsify-side restrict-only witness (lead-coder's own
# required condition, verbatim): parent ceiling=standard, child declares
# strong -> reject; child narrows to light -> allowed. Composed at the
# compose_model_ceiling level, fed straight into the SAME enforcement
# predicate (model_class_exceeds_ceiling) recorded_acompletion's #1190
# chokepoint already uses — proving the composed value, not just the raw
# layers, is what a "strong" call gets rejected against.
# ---------------------------------------------------------------------------


def test_child_declaring_a_wider_class_than_parent_is_still_rejected() -> None:
    """Tier 2: falsify direction — parent(project) ceiling=standard, child
    (agent/session layer) declares strong: the COMPOSED ceiling is still
    standard (restrict-only held), so a "strong"-class call is REJECTED by
    the same predicate the #1190 chokepoint enforces with."""
    composed = compose_model_ceiling("standard", "strong")
    assert composed == "standard"
    assert model_class_exceeds_ceiling("strong", composed) is True


def test_child_narrowing_to_a_lighter_class_is_allowed() -> None:
    """Tier 2: accept direction (the reverse leg of the same required
    test) — parent ceiling=standard, child narrows to light: the composed
    ceiling follows the child, so a "light"-class call is ALLOWED."""
    composed = compose_model_ceiling("standard", "light")
    assert composed == "light"
    assert model_class_exceeds_ceiling("light", composed) is False


# ---------------------------------------------------------------------------
# AgentProfile.bounding — round-trip + validation (Tier 1)
# ---------------------------------------------------------------------------


def test_agent_profile_bounding_round_trips_through_save_and_load(tmp_path: Path) -> None:
    """Tier 1: contract — a profile's bounding mapping survives a
    save()/load() cycle unchanged, same shape as preferences'."""
    profile = AgentProfile.new("bounder-1")
    object.__setattr__(profile, "bounding", {"model": "standard"})
    agent_dir = tmp_path / "agents" / "bounder-1"
    profile.save(agent_dir)

    loaded = AgentProfile.load(agent_dir)
    assert loaded.bounding == {"model": "standard"}


def test_agent_profile_bounding_empty_by_default_and_omitted_from_yaml(tmp_path: Path) -> None:
    """Tier 1: accept-side — the common case (no bounding declared) stays
    {} and the on-disk YAML omits the key entirely, matching preferences'
    own "empty dict, not None, minimal on-disk shape" contract."""
    profile = AgentProfile.new("bounder-2")
    agent_dir = tmp_path / "agents" / "bounder-2"
    profile.save(agent_dir)

    raw = yaml.safe_load((agent_dir / "profile.yaml").read_text(encoding="utf-8"))
    assert "bounding" not in raw

    loaded = AgentProfile.load(agent_dir)
    assert loaded.bounding == {}


def test_agent_profile_load_raises_on_unknown_bounding_key(tmp_path: Path) -> None:
    """Tier 1: contract — a profile.yaml with a bounding key outside
    BOUNDING_KEYS raises UnknownBoundingKeyError at load() time."""
    agent_dir = tmp_path / "agents" / "bounder-3"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        yaml.safe_dump({
            "name": "bounder-3", "role": "", "created_at": "",
            "bounding": {"router_max_iterations": 10},
        }),
        encoding="utf-8",
    )
    with pytest.raises(UnknownBoundingKeyError):
        AgentProfile.load(agent_dir)


# ---------------------------------------------------------------------------
# Session.model_class_ceiling — live 3-layer composition (Tier 2)
# ---------------------------------------------------------------------------


def _agent_dir(session) -> Path:
    return session.workspace_dir


def _session_config_path(session) -> Path:
    return Path(session._snapshot_path).parent / "config.yaml"


def _write_agent_bounding(session, name: str, bounding: dict) -> None:
    profile = AgentProfile.new(name)
    object.__setattr__(profile, "bounding", bounding)
    profile.save(_agent_dir(session))


def test_model_class_ceiling_is_project_only_with_no_layers_set(tmp_path: Path) -> None:
    """Tier 2: accept-side — no agent/session bounding override, the
    project resolver's own class_ceiling() is the effective value,
    byte-identical to before this slice (RouterLoop's prior
    construction-time-cached read)."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    session = make_session(
        agent_name="bound-1", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    assert session.model_class_ceiling == "standard"


def test_model_class_ceiling_project_unbounded_with_no_layers_set(tmp_path: Path) -> None:
    """Tier 2: accept-side — no project ceiling AND no agent/session
    override at all -> fully unbounded (None), the compat default."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"})  # model_max_class unset
    session = make_session(
        agent_name="bound-2", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    assert session.model_class_ceiling is None


def test_model_class_ceiling_agent_layer_widening_is_rejected(tmp_path: Path) -> None:
    """Tier 2: THE mandatory falsify witness at Session level — project
    ceiling=standard, agent-layer bounding.model=strong (a WIDER
    declaration): the effective ceiling stays standard, the child cannot
    widen it."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    session = make_session(
        agent_name="bound-3", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    _write_agent_bounding(session, "bound-3", {"model": "strong"})

    assert session.model_class_ceiling == "standard"


def test_model_class_ceiling_agent_layer_narrowing_is_allowed(tmp_path: Path) -> None:
    """Tier 2: THE reverse leg — project ceiling=standard, agent-layer
    bounding.model=light (a NARROWER declaration): the effective ceiling
    follows the child down to light."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    session = make_session(
        agent_name="bound-4", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    _write_agent_bounding(session, "bound-4", {"model": "light"})

    assert session.model_class_ceiling == "light"


def test_model_class_ceiling_session_layer_also_cannot_widen(tmp_path: Path) -> None:
    """Tier 2: falsify witness, session-layer leg — project ceiling=
    standard, session-layer config.yaml bounding.model=strong: still
    rejected (composed stays standard)."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    session = make_session(
        agent_name="bound-5", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    cfg_path = _session_config_path(session)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"name": "_session_x", "bounding": {"model": "strong"}}),
        encoding="utf-8",
    )

    assert session.model_class_ceiling == "standard"


def test_model_class_ceiling_narrowest_of_all_three_layers_wins(tmp_path: Path) -> None:
    """Tier 2: composition witness — project=strong (unbounded-ish, the
    widest tier), agent narrows to standard, session narrows further to
    light: the NARROWEST of all three layers wins, matching
    compose_model_ceiling's own three-layer pure-function test above."""
    resolver = ModelResolver({"strong": "anthropic/claude-opus-5"}, model_max_class="strong")
    session = make_session(
        agent_name="bound-6", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    _write_agent_bounding(session, "bound-6", {"model": "standard"})
    cfg_path = _session_config_path(session)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"name": "_session_x", "bounding": {"model": "light"}}),
        encoding="utf-8",
    )

    assert session.model_class_ceiling == "light"


def test_model_class_ceiling_is_a_live_re_read(tmp_path: Path) -> None:
    """Tier 2: accept-side — editing profile.yaml AFTER Session
    construction takes effect on the next access, same live-re-read shape
    output_language/reasoning_display/warn_ratio_overrides already use."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    session = make_session(
        agent_name="bound-7", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    assert session.model_class_ceiling == "standard"

    _write_agent_bounding(session, "bound-7", {"model": "light"})

    assert session.model_class_ceiling == "light"


# ---------------------------------------------------------------------------
# RouterHostAdapter.model_class_ceiling() — callback wiring (Tier 2)
# ---------------------------------------------------------------------------


def test_model_class_ceiling_fn_none_falls_back_to_resolver_class_ceiling() -> None:
    """Tier 2: accept-side — a RouterHostAdapter built WITHOUT
    model_class_ceiling_fn (every pre-② caller, every existing test host)
    falls back to the resolver's own class_ceiling(), byte-identical to
    before this slice."""
    from tests._support.router_host_adapter import make_adapter

    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    adapter = make_adapter(universal_wrappers_enabled=False, resolver=resolver)
    assert adapter.model_class_ceiling() == "standard"


def test_router_host_model_class_ceiling_reflects_the_live_session_composition(tmp_path: Path) -> None:
    """Tier 2: THE end-to-end witness — RouterHostAdapter.model_class_ceiling()
    (what RouterLoop actually consults) reflects the SAME live composed
    value as Session.model_class_ceiling, via the model_class_ceiling_fn
    callback wired at construction."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"}, model_max_class="standard")
    session = make_session(
        agent_name="bound-8", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )
    assert session.router_host.model_class_ceiling() == "standard"

    _write_agent_bounding(session, "bound-8", {"model": "light"})

    assert session.model_class_ceiling == "light"
    assert session.router_host.model_class_ceiling() == "light"


# ---------------------------------------------------------------------------
# End-to-end: RouterLoop -> recorded_acompletion enforcement (Tier 2)
# ---------------------------------------------------------------------------


def test_router_loop_rejects_a_call_whose_class_exceeds_the_composed_ceiling(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: THE full end-to-end falsify witness — a real turn through
    RouterLoop.run, driven by a real Session whose project resolver
    declares a "strong" default router class but a "standard" ceiling:
    ModelClassExceedsCeilingError is raised BEFORE litellm.acompletion is
    ever invoked (recorded_acompletion's own #1190 chokepoint), proving
    the LIVE per-call composition (not just the construction-time-cached
    value RouterLoop used before ②) actually reaches enforcement."""
    from reyn.runtime.router_loop import RouterLoop

    called = {"n": 0}

    async def _spy(model, messages, **kw):  # noqa: ANN001, ANN003
        called["n"] += 1
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    monkeypatch.setattr(litellm, "acompletion", _spy)

    resolver = ModelResolver(
        {"strong": "anthropic/claude-opus-5"},
        default_class="strong",
        model_max_class="standard",
    )
    session = make_session(
        agent_name="bound-9", workspace_state_dir=tmp_path / ".reyn", resolver=resolver,
    )

    with pytest.raises(ModelClassExceedsCeilingError) as excinfo:
        asyncio.run(RouterLoop(host=session.router_host, chain_id="c1").run("hi", []))

    assert called["n"] == 0, "litellm.acompletion must never be invoked on a rejected call"
    assert excinfo.value.requested == "strong"
    assert excinfo.value.ceiling == "standard"
