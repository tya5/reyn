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


def test_a_model_class_stream_field_is_accepted_and_consumed():
    """Tier 1: an operator-declared ``stream:`` loads, and does NOT reach kwargs.

    This assertion is inverted from what #3627 pinned, by owner decision: an
    operator who configured the endpoint can know things litellm's pinned
    snapshot does not, so they get to state the answer. What #3627 was
    protecting against — the key riding ``spec.kwargs`` into
    ``litellm.acompletion`` — is closed more firmly than by rejecting it:
    ``stream`` is a consumed ModelSpec field now, so there is no longer a
    kwargs path for it to take. The construction-time rejection above still
    stands for anything that puts one there directly.
    """
    resolver = ModelResolver(
        {"gpt-5.6-luna": {"model": "gpt-5.6-luna", "stream": True}}
    )
    spec = resolver.resolve("gpt-5.6-luna")

    assert spec.stream is True
    assert "stream" not in spec.kwargs


def test_the_stream_field_survives_the_reyn_config_models_layer():
    """Tier 1: it arrives through the real ``reyn.local.yaml`` load path.

    Asserted through ``ReynConfig.llm.models`` -> ``ModelResolver`` rather than
    a hand-built mapping, because an operator writes YAML — a field that
    parsed in isolation but was dropped by the config layer would look
    identical from a unit test and do nothing in a real run.

    #4174 T3: ``models`` moved from a top-level ``ReynConfig`` field to
    ``ReynConfig.llm.models``.
    """
    import dataclasses

    from reyn.config import LLMConfig, ReynConfig

    cfg = dataclasses.replace(ReynConfig(), llm=LLMConfig(models={
        "gpt-5.6-luna": {"model": "gpt-5.6-luna", "stream": True},
    }))

    spec = ModelResolver(cfg.llm.models).resolve("gpt-5.6-luna")

    assert spec.stream is True
    assert "stream" not in spec.kwargs


def test_stream_key_accepted_and_consumed_through_extends_merge_path():
    """Tier 1: #4689 — inverted (again) from what this test used to pin, for
    the SAME reason `test_a_model_class_stream_field_is_accepted_and_consumed`
    above already inverted the plain-`from_config` case: the owner decision
    that made `stream` a real, accepted ModelSpec field applies uniformly
    to every producer of one, not just `from_config`. Before #4689, the
    `extends`-merge path bypassed `from_config`'s field extraction entirely
    (a direct `ModelSpec(model=model, kwargs=merged)` call), so `stream`
    landed in `kwargs` unextracted and tripped THIS file's own rejection
    guard — an inconsistency with the plain-dict path, not an intentional
    stricter rule for `extends` specifically. #4689 routes the `extends`
    path through `from_config` too (needed for that PR's own
    `max_input_tokens` field to propagate through `extends` at all),
    which closes the inconsistency as a side effect: `stream` via `extends`
    now behaves identically to `stream` via a plain dict."""
    resolver = ModelResolver({
        "base": {"model": "openai/gpt-4o"},
        "child": {"extends": "base", "stream": True},
    })
    spec = resolver.resolve("child")
    assert spec.stream is True
    assert "stream" not in spec.kwargs


def test_no_stream_key_is_unaffected():
    """Tier 1: #3627 — a model def without `stream`/`stream_options` is
    unchanged (the validation is a no-op; the passthrough policy for other
    kwargs, e.g. temperature, is intact)."""
    spec = ModelSpec(model="openai/gpt-4o", kwargs={"temperature": 0.2})
    assert spec.kwargs == {"temperature": 0.2}
