"""#3627: an operator-declared ``stream`` (or ``stream_options``) on a
``models:`` entry must fail at config-load, not ride the ``spec.kwargs``
passthrough into ``litellm.acompletion``.

Reyn owns the streaming decision (``llm.py``'s single completion funnel makes
it per-call via a litellm capability query inside ``recorded_acompletion``);
a settable ``stream`` key on a model def is INERT as an enable (the gate
still decides) and ACTIVE as a break — on the collect-whole branch it makes
litellm return a ``CustomStreamWrapper`` that the branch reads as a finished
response, surfacing as ``EmptyLLMResponseError ... provider response:
<CustomStreamWrapper object ...>`` (an error naming neither ``stream`` nor
the config).

Reachability note (owner decision, #3627 comment thread): ``ModelSpec.__post_init__``
is the SINGLE construction-time validation site — every producer of a
``ModelSpec`` with non-empty ``kwargs`` routes through it (``from_config``,
the ``extends``-merge path, and every direct ``ModelSpec(...)`` call site in
``llm.py`` / ``router_loop.py``, all of which pass either ``kwargs={}`` or a
reyn-internal hardcoded dict that never contains ``stream``). Once this
rejection lands, there is no remaining path that gets a ``stream`` key into
``spec.kwargs`` reaching ``llm.py``'s collect-whole branch — so a SEPARATE
strip immediately before ``**call_kwargs`` there would be a declared,
implemented, tested branch that is never called (verification-hazards.md
§15). This PR does not add that strip; see the PR body for the full
enumeration.

Tier: config-parse + load-time validation (fail-fast) = Tier 1 (the
ModelSpec config contract, same class as #1650's reasoning_effort tests).
"""
from __future__ import annotations

import pytest

from reyn.llm.model_resolver import ModelResolver, ModelSpec

# ── Tier 1: load-time rejection (fail-fast) ─────────────────────────────────


def test_stream_key_rejected_at_construction():
    """Tier 1: #3627 — ``stream`` on a model def fails at ModelSpec
    construction (config-load), not mid-call inside litellm."""
    with pytest.raises(ValueError, match="stream") as excinfo:
        ModelSpec(model="openai/gpt-5.6-luna", kwargs={"stream": True})
    msg = str(excinfo.value)
    # Decision-enabling: names WHO decides (reyn, not the operator) and WHAT
    # to do (remove it) — not just "invalid key".
    assert "reyn decides" in msg
    assert "model=" in msg and "gpt-5.6-luna" in msg


def test_stream_options_key_also_rejected():
    """Tier 1: #3627 — ``stream_options`` (the sibling litellm streaming
    control) is rejected the same way as ``stream``."""
    with pytest.raises(ValueError, match="stream_options"):
        ModelSpec(model="openai/gpt-5.6-luna", kwargs={"stream_options": {"include_usage": True}})


def test_stream_rejection_message_explains_the_failure_mode():
    """Tier 1: #3627 — the message describes what goes wrong when the key is
    set (a stream object read as a finished reply), matching the legibility
    standard of the existing reasoning_effort deny messages in this file,
    not a bare "invalid key"."""
    with pytest.raises(ValueError) as excinfo:
        ModelSpec(model="openai/gpt-5.6-luna", kwargs={"stream": True})
    msg = str(excinfo.value)
    assert "CustomStreamWrapper" in msg


def test_stream_key_rejected_at_resolver_startup():
    """Tier 1: #3627 — the fail-fast also fires through ModelResolver
    startup (the path a real reyn.local.yaml `models:` entry takes),
    naming the offending model."""
    with pytest.raises(ValueError, match="gpt-5.6-luna"):
        ModelResolver(
            {"gpt-5.6-luna": {"model": "gpt-5.6-luna", "stream": True}}
        )


def test_stream_key_rejected_through_reyn_config_models_layer():
    """Tier 1: #3627 — the rejection fires through ``ReynConfig.models`` ->
    ``ModelResolver`` (the actual reyn.local.yaml `models:` load path), not
    just via a hand-constructed ModelSpec/mapping."""
    from reyn.config import ReynConfig

    cfg = ReynConfig(models={
        "gpt-5.6-luna": {"model": "gpt-5.6-luna", "stream": True},
    })
    with pytest.raises(ValueError, match="reyn decides"):
        ModelResolver(cfg.models)


def test_stream_key_rejected_through_extends_merge_path():
    """Tier 1: #3627 — a `stream` introduced via an `extends` merge (the
    OTHER ModelSpec producer besides `from_config`) is rejected too — single
    validation site (`__post_init__`) covers both producers by construction."""
    with pytest.raises(ValueError, match="reyn decides"):
        ModelResolver({
            "base": {"model": "openai/gpt-4o"},
            "child": {"extends": "base", "stream": True},
        })


def test_no_stream_key_is_unaffected():
    """Tier 1: #3627 — a model def without `stream`/`stream_options` is
    unchanged (the validation is a no-op; the passthrough policy for other
    kwargs, e.g. temperature, is intact)."""
    spec = ModelSpec(model="openai/gpt-4o", kwargs={"temperature": 0.2})
    assert spec.kwargs == {"temperature": 0.2}
