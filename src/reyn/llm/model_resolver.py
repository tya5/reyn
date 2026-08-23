"""
ModelResolver: resolves model class names to LiteLLM model strings.

Standard classes: light, standard, strong.
Mapping is provided by ReynConfig.llm.models (loaded from reyn.yaml, #4174 T3:
moved from top-level ReynConfig.models).
A name in a NAME position (contains ``/``) passes through unchanged (backward
compatible with raw LiteLLM strings); a name in a CLASS position (no ``/``)
that fails to resolve raises — see ``resolve()``'s own docstring (#4349).

PR-MODEL-SPEC-EXTENDS: adds ``extends`` field support.

Disambiguation heuristic for str-form values:
  - value contains ``/``  -> literal LiteLLM model string (backward compat)
  - value has no ``/``    -> class reference shorthand (resolved via namespace)

#4349: there is no built-in MODEL catalog anymore — owner ruling (c), "the
class of thing that grows (concrete provider/model targets) leaves the code;
the class that doesn't (the 3 tier NAMES and their cost ORDER, reyn's own
vocabulary) stays." A project maps its own tier targets under `llm.models:`
in reyn.yaml (the project template `reyn init` writes already does this —
see ``interfaces/cli/templates.py``); with no config at all, `light`/
`standard`/`strong` are simply unresolved class names, which now raises a
legible error (the #3368 class of confusing-error this used to paper over
for exactly 3 names, leaving every OTHER typo/load-failure just as opaque —
see ``resolve()``).

Startup fail-fast: all entries are resolved at ``__init__`` time.  Unresolvable
references, cycles, and missing ``model`` fields raise ``ValueError``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The standard model tiers, in ascending cost order (light < standard <
#: strong) — reyn's own vocabulary (the CLI's `--model` flag, `ReynConfig.
#: llm.model`'s default, #4206 T1's ceiling comparison), not a third-party
#: catalog: unlike a concrete provider/model target, this set does not grow
#: every time a provider adds a model (#4349, owner ruling (c) — the
#: judgment call from #4347/#4348: "does reyn have to add code every time
#: the THIRD PARTY grows?" here the answer is no, so it stays a plain literal
#: rather than moving to config).
STANDARD_CLASSES = ("light", "standard", "strong")


def model_class_exceeds_ceiling(class_name: str, ceiling: "str | None") -> bool:
    """#4206 T1 (②bounding): True iff *class_name* is strictly MORE expensive
    than *ceiling*, per ``STANDARD_CLASSES``'s own cost order (light <
    standard < strong — the same order ``BUILTIN_TIER_ALIASES``, its single
    producer, already declares its keys in).

    ``ceiling=None`` means no ceiling is configured (the compat default —
    every other bounding axis in reyn defaults to unbounded) -> always
    False, never a violation. A *class_name* or *ceiling* that is not one
    of the three known tiers (e.g. a raw ``provider/model`` string, or a
    project-declared custom class name outside light/standard/strong) is
    NOT comparable on this axis -> also False: this predicate can only
    judge violations it can actually order, never guess. Callers that need
    a ceiling to also constrain custom class names would need a richer
    per-class cost declaration — out of #4206 T1's scope (one axis: the 3
    standard tiers).

    Scope limit (#4324, part of #4206): this axis only ever sees a class
    when the CALLER resolved one via ``class_for_purpose``/``model_class``.
    A caller that received an explicit raw model string (e.g.
    ``--router-model openai/gpt-4o`` bypassing class resolution entirely)
    has nothing to compare — the ceiling is a silent no-op for that call, by
    construction, not a bug in this predicate. Likewise a call that declared
    ``model_class=None`` (dogfood's real-cost calls included, not just
    compaction) is permanently outside the axis. "a ceiling is configured"
    does NOT mean "every LLM spend is bounded" — only class-resolved calls
    are.
    """
    if ceiling is None:
        return False
    if class_name not in STANDARD_CLASSES or ceiling not in STANDARD_CLASSES:
        return False
    return STANDARD_CLASSES.index(class_name) > STANDARD_CLASSES.index(ceiling)


#: #1650: valid values for the per-model ``reasoning_effort`` field. These are
#: litellm's accepted reasoning-effort levels for the gemini provider, each of
#: which maps to a native thinking budget (low→1024, medium→2048, high→4096,
#: minimal→model-specific, disable/none→0; verified live against litellm 1.84.0
#: gemini/gemini-2.5-flash-lite). Validated at config-load (fail-fast) so a typo
#: surfaces at startup instead of raising mid-call inside litellm.
VALID_REASONING_EFFORTS: frozenset = frozenset(
    {"minimal", "low", "medium", "high", "disable", "none"}
)


@dataclass(frozen=True)
class ModelSpec:
    """Resolved model configuration for a single model class.

    ``model`` is the LiteLLM model string (required).
    ``kwargs`` carries any additional litellm kwargs declared by the operator
    in reyn.yaml (e.g. temperature, max_tokens, extra_body).  These are
    silently passed through to litellm — unknown fields are not validated
    (passthrough policy, Q1 Option B).
    """

    model: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    # #309 per-class routing (multi-provider). Explicit routing fields (NOT in
    # ``kwargs``) so a class can target its own endpoint/provider while others
    # use the global default — e.g. router=light on a Gemini proxy + strong=
    # capable on Anthropic-direct, simultaneously. Mirrors EmbeddingClassSpec
    # (which carries ``api_base``); ``provider`` is added for the direct-vs-proxy
    # opt-out. NO per-class api_key field by design: a literal secret must never
    # live in checked-in config — litellm resolves the key from the standard
    # provider env (OPENAI_API_KEY for a proxy, ANTHROPIC_API_KEY/GEMINI_API_KEY
    # for a direct provider). Both default None → inherit the global api_base.
    api_base: str | None = None
    provider: str | None = None  # litellm custom_llm_provider
    # Whether calls for this class stream. A REYN field consumed by
    # ``_streaming_enabled`` — never forwarded to litellm, which is the whole
    # point: passed through as an ordinary kwarg it reaches the collect-whole
    # branch and makes litellm return a stream object that reyn then reads as a
    # finished reply (#3627). ``None`` = no operator opinion, decide from the
    # catalog.
    stream: bool | None = None
    # #4689 (owner instruction): operator-declared CEILING OVERRIDE for
    # ``reyn.llm.model_budget.get_max_input_tokens`` — takes priority over
    # the litellm catalog lookup unconditionally (not just when the catalog
    # lookup fails). A REYN field, same reasoning as ``stream`` above: left
    # in ``kwargs`` it would silently pass through to litellm as an unknown
    # completion kwarg (accepted, does nothing) — the #4655 B-3 shape this
    # field exists specifically to NOT reproduce. ``None`` = no operator
    # opinion, defer to the catalog (byte-identical to before this field
    # existed). See ``ModelResolver.max_input_token_overrides`` for how this
    # reaches ``get_max_input_tokens`` without threading a resolver/class
    # name through every one of its 8 call sites.
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        # #1650: validate the operator-declared ``reasoning_effort`` at
        # config-load (fail-fast) rather than letting an invalid value reach
        # litellm and raise mid-call. The value itself rides the existing
        # ``kwargs`` passthrough to litellm.acompletion (call_llm:1155 /
        # call_llm_tools:1359), which maps it to the provider's native thinking
        # budget — we only gate the value set + the conflicting-config combo.
        # Placed here (not in from_config) so BOTH the plain-dict path and the
        # extends-merge path (which build ModelSpec directly) are covered
        # by-construction — single validation site, no parallel drift.
        # #3627, revised by #3639: ``stream`` on a model class is now a REYN
        # field (see ``ModelSpec.stream``), consumed by ``_streaming_enabled``
        # as the operator's answer — an operator who configured the endpoint
        # can know what litellm's pinned snapshot does not. It never reaches
        # ``kwargs``, so the passthrough this guard was written for has no path
        # left; the guard stays as a construction-time backstop for anything
        # that puts one there directly, and for ``stream_options``, which has
        # no reyn meaning and breaks the same way. The original reasoning,
        # still accurate for a kwargs-borne key: `llm.py`'s single completion funnel makes that call
        # per-request via a litellm capability query
        # (`_streaming_enabled`, inside `recorded_acompletion`) and
        # deliberately sets no `stream` key of its own at the call site the
        # operator's kwargs ride into. A `stream: true` here does NOT turn
        # streaming on (the gate still decides that) — it rides
        # `spec.kwargs` straight through to `litellm.acompletion` on
        # whichever branch the gate picks. On the collect-whole branch
        # (gate says no) that returns a `CustomStreamWrapper` instead of a
        # completed response, which the branch then reads as one, surfacing
        # as `EmptyLLMResponseError: LLM returned a 200 response with empty
        # choices ...; provider response: <CustomStreamWrapper object ...>`
        # — an error that names neither `stream` nor the config. Reject at
        # load, before that shape can ever form.
        if "stream" in self.kwargs or "stream_options" in self.kwargs:
            _bad = [k for k in ("stream", "stream_options") if k in self.kwargs]
            raise ValueError(
                f"models {'/'.join(_bad)} must not be set (model={self.model!r}); "
                "reyn decides whether a call streams for itself (a per-call "
                "litellm capability query inside recorded_acompletion) — an "
                "operator-set 'stream' does not turn streaming on, it only "
                "rides through to litellm.acompletion on whichever branch "
                "the gate picks. On the non-streaming branch this makes "
                "litellm return a stream object that reyn then reads as a "
                "finished reply, surfacing as "
                "'EmptyLLMResponseError ... provider response: "
                "<CustomStreamWrapper ...>'. Remove it from this model's "
                "config; reyn will stream when it decides the model/call "
                "shape supports it."
            )

        effort = self.kwargs.get("reasoning_effort")
        if effort is None:
            return
        # #1654: two accepted forms —
        #   - str (gemini native): the effort level directly, e.g. "low".
        #   - dict (OpenAI/GPT-5 summary opt-in): {"effort": <level>, "summary":
        #     "detailed"}. OpenAI reasoning models don't return reasoning TEXT
        #     unless a summary is requested; litellm's GPT-5 transformation reads
        #     {effort, summary} (gpt_5_transformation.py). The level is validated
        #     either way; the optional "summary" rides through to litellm.
        if isinstance(effort, dict):
            level = effort.get("effort")
        elif isinstance(effort, str):
            level = effort
        else:
            level = None
        if level not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"models reasoning_effort must be one of "
                f"{sorted(VALID_REASONING_EFFORTS)} (a string), or a dict with "
                f"'effort' one of them (e.g. {{'effort': 'low', 'summary': "
                f"'detailed'}} for OpenAI summary text); got {effort!r} "
                f"(model={self.model!r})"
            )
        # Both-set reject: reasoning_effort already maps to a native thinking
        # budget, so a hand-set extra_body thinking config is a contradictory
        # second control (litellm raises UnsupportedParamsError on a
        # thinking_level+budget conflict). Reject at load so the operator picks
        # exactly one.
        extra_body = self.kwargs.get("extra_body")
        if isinstance(extra_body, dict) and (
            "thinking_config" in extra_body or "thinkingConfig" in extra_body
        ):
            raise ValueError(
                f"models cannot set both reasoning_effort and an extra_body "
                f"thinking config (model={self.model!r}); reasoning_effort "
                f"already maps to the native thinking budget — set only one."
            )

    @classmethod
    def from_config(cls, value: object) -> "ModelSpec":
        """Parse a reyn.yaml llm.models: entry into a ModelSpec.

        #4174 T3: moved from top-level `models:` — same entry shape.

        Accepted forms:
          str  -> ModelSpec(model=value, kwargs={})
          dict -> model field is required; remaining fields go to kwargs.
                  ``extends`` is stripped (handled by ModelResolver, not here).

        Raises:
          ValueError  if value is a dict without a ``model`` key, or if
                      value is neither str nor dict.
        """
        if isinstance(value, str):
            return cls(model=value, kwargs={})
        if isinstance(value, dict):
            if "model" not in value:
                raise ValueError(
                    "ModelSpec dict form requires a 'model' key; "
                    f"got keys: {list(value.keys())}"
                )
            model = value["model"]
            if not isinstance(model, str):
                raise ValueError(
                    f"ModelSpec 'model' must be a string, got {type(model).__name__}"
                )
            # #309: api_base / provider are explicit routing fields, not litellm
            # passthrough kwargs. (No api_key: literal secrets never in config —
            # litellm reads the key from the standard provider env.)
            api_base = value.get("api_base")
            provider = value.get("provider")
            if api_base is not None and not isinstance(api_base, str):
                raise ValueError(
                    f"ModelSpec 'api_base' must be a string, got {type(api_base).__name__}"
                )
            if provider is not None and not isinstance(provider, str):
                raise ValueError(
                    f"ModelSpec 'provider' must be a string, got {type(provider).__name__}"
                )
            stream = value.get("stream")
            if stream is not None and not isinstance(stream, bool):
                raise ValueError(
                    f"ModelSpec 'stream' must be a boolean, got {type(stream).__name__}"
                )
            # #4689: same explicit-field treatment as api_base/provider/stream
            # above — left in kwargs it would silently pass through to
            # litellm as an unrecognized completion kwarg (accepted, does
            # nothing — see ModelSpec.max_input_tokens's own docstring).
            max_input_tokens = value.get("max_input_tokens")
            if max_input_tokens is not None:
                if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int):
                    raise ValueError(
                        "ModelSpec 'max_input_tokens' must be an int, got "
                        f"{type(max_input_tokens).__name__}"
                    )
                if max_input_tokens <= 0:
                    raise ValueError(
                        f"ModelSpec 'max_input_tokens' must be positive, got {max_input_tokens}"
                    )
            kwargs = {
                k: v for k, v in value.items()
                if k not in (
                    "model", "extends", "api_base", "provider", "stream",
                    "max_input_tokens",
                )
            }
            return cls(
                model=model, kwargs=kwargs, api_base=api_base,
                provider=provider, stream=stream,
                max_input_tokens=max_input_tokens,
            )
        raise ValueError(
            f"ModelSpec.from_config expects str or dict, got {type(value).__name__}"
        )


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict.

    - When both base[k] and override[k] are dicts, recurse.
    - Otherwise override[k] replaces base[k] (including lists and scalars).
    - Keys present only in base are carried unchanged.
    - Keys present only in override are added.
    """
    result = dict(base)
    for key, override_val in override.items():
        base_val = result.get(key)
        if isinstance(base_val, dict) and isinstance(override_val, dict):
            result[key] = _deep_merge(base_val, override_val)
        else:
            result[key] = override_val
    return result


class ModelClassExceedsCeilingError(ValueError):
    """#4206 T1: raised by :func:`reyn.llm.llm.recorded_acompletion` when a
    call's declared ``model_class`` exceeds the caller's effective
    ``model_class_ceiling`` (##bounding axis — restrict-only, reject-not-
    clamp, same shape as #3903①'s ``timeout_seconds`` ceiling). Carries the
    actual ceiling so the caller can build a legible error naming it,
    mirroring #3903①'s "reject with the real max named" contract rather
    than a bare denial."""

    def __init__(self, requested: str, ceiling: str, purpose: str) -> None:
        self.requested = requested
        self.ceiling = ceiling
        self.purpose = purpose
        super().__init__(
            f"model class {requested!r} (purpose={purpose!r}) exceeds this "
            f"session's configured ceiling of {ceiling!r}. Use {ceiling!r} "
            f"or a cheaper class, or ask the operator to raise the ceiling."
        )


def model_family(model: str) -> str:
    """#1791 A2: coarse model-family classifier for SP gating (the SINGLE place
    family is classified — do NOT scatter ``"gemini" in x`` across SP builders).
    Model names are not domain-specific strings, so this is P7-OK. Operates on the
    resolved LiteLLM model string (e.g. ``"openai/gemini-2.5-flash-lite"`` →
    ``"gemini"`` — the family is in the name regardless of the proxy prefix).
    Returns one of ``"claude" | "gemini" | "gpt" | "other"``.
    """
    m = (model or "").lower()
    if "claude" in m or "anthropic" in m:
        return "claude"
    if "gemini" in m or "gemma" in m:
        return "gemini"
    if "gpt" in m or "codex" in m or "/o1" in m or "/o3" in m:
        return "gpt"
    return "other"


def _spec_to_dict(spec: ModelSpec) -> dict[str, Any]:
    """Convert a ModelSpec back to a flat dict for merging (the ``extends``
    resolution path's base-class side). Carries every explicit ModelSpec
    field (``api_base``/``provider``/``stream``/``max_input_tokens``), not
    just ``model``/``kwargs`` — #4689: a prior revision omitted these,
    which meant a base class's ``stream``/``api_base``/``provider`` never
    reached an ``extends``-ing override at all (the field simply vanished
    on that path, even though the override side round-tripped correctly
    through ``ModelSpec.from_config``). Only a set (non-``None``) field is
    included, so an override's own value is never masked by a base's
    unset default via ``_deep_merge``."""
    result: dict[str, Any] = {"model": spec.model}
    result.update(spec.kwargs)
    if spec.api_base is not None:
        result["api_base"] = spec.api_base
    if spec.provider is not None:
        result["provider"] = spec.provider
    if spec.stream is not None:
        result["stream"] = spec.stream
    if spec.max_input_tokens is not None:
        result["max_input_tokens"] = spec.max_input_tokens
    return result


def _strip_keys(d: dict, keys: set[str]) -> dict:
    """Return a copy of *d* with the given *keys* removed."""
    return {k: v for k, v in d.items() if k not in keys}


def resolve_purpose_class(
    explicit: "str | None", resolver: "ModelResolver | None", purpose: str,
) -> str:
    """#1672: pick the model CLASS for a *purpose*, with explicit-wins precedence.

    An ``explicit`` (caller-supplied) value wins; otherwise the *resolver*'s
    per-purpose class (``class_for_purpose``, which falls back to the configured
    default class); otherwise ``"standard"`` when no config-aware resolver is
    available (a stub / no-resolver context — byte-identical to the former
    hardcodes). Shared by ``RouterLoop`` (router) + the planner so the chat router
    and plan-decomposition router resolve identically from one place.
    """
    if explicit is not None:
        return explicit
    if resolver is not None and hasattr(resolver, "class_for_purpose"):
        return resolver.class_for_purpose(purpose)
    return "standard"


class ModelResolver:
    def __init__(
        self,
        mapping: dict[str, Any],
        *,
        default_class: str = "standard",
        purpose_classes: dict[str, str] | None = None,
        model_max_class: "str | None" = None,
    ) -> None:
        """Build a ModelResolver.

        Args:
            mapping:  User-declared models from reyn.yaml (``ReynConfig.llm.models``,
                      #4174 T3: moved from top-level ``.models``). The sole
                      source of the namespace (#4349: reyn ships no built-in
                      model catalog to merge underneath it — an earlier
                      ``builtin=`` parameter existed for exactly that merge
                      and was removed with the catalog itself once
                      measurement showed zero production call sites ever
                      passed it; a caller that wants a synthetic namespace
                      entry for a test declares it directly in ``mapping``).
            default_class: #1672 — the configured default model class
                      (``ReynConfig.llm.model``, #4174 T3: moved from top-level
                      ``.model``) returned by ``class_for_purpose`` for any unset
                      purpose. Defaults to ``"standard"`` so resolvers built
                      without it stay byte-identical to the old hardcodes.
            purpose_classes: #1672 — per-purpose class overrides
                      (``ReynConfig.llm.model_class_by_purpose``, #4174 T3: moved
                      from top-level ``.model_class_by_purpose``). A purpose
                      present here wins over ``default_class`` in
                      ``class_for_purpose``.
            model_max_class: #4206 T1 (②bounding, ``model`` key) — the
                      project-declared ceiling (``ReynConfig.llm.model_max_class``).
                      ``None`` (default) means unbounded, byte-identical to
                      before this field existed. Exposed via ``class_ceiling()``;
                      enforcement itself happens at ``recorded_acompletion``, not
                      here — this class only carries the configured value.
        """
        self._model_max_class = model_max_class
        self._default_class = default_class
        self._purpose_classes: dict[str, str] = dict(purpose_classes or {})

        # Flat namespace: mapping is the only source (#4349).
        self._namespace: dict[str, Any] = dict(mapping)

        # Resolve all entries at startup (fail-fast).
        self._resolved: dict[str, ModelSpec] = {}
        for name in self._namespace:
            if name not in self._resolved:
                self._resolved[name] = self._resolve_entry(
                    name, self._namespace[name], frozenset()
                )

        # #1454 PR-B: name-position validation. The resolved ``model`` is a NAME
        # position, which should be ``provider/model`` (the `/`-prefix invariant).
        # WARN (not error) for a bare name:
        # litellm may accept some bare strings, so bare usage is
        # degraded-but-allowed, flagged so a misroute is diagnosable. (Class
        # positions — tier references — are closed-world via
        # resolve_class_or_fallback; this is the name-position leg of the same
        # unified class/name rule, shared with embedding.classes[*].model.)
        for _name, _spec in self._resolved.items():
            if "/" not in _spec.model:
                import logging

                logging.getLogger(__name__).warning(
                    "models.%s model %r has no provider prefix ('/') — a model "
                    "position should be 'provider/model' (e.g. 'openai/gpt-4o'). "
                    "Treating as a bare LiteLLM name; add the prefix if "
                    "resolution misroutes.",
                    _name, _spec.model,
                )

        # #4573 (architect ruling): WARN at LOAD time — not raise — for a
        # class-position value this resolver cannot resolve. Load-bearing
        # ``resolve()`` (below) still raises at USE time, unchanged — #3368's
        # own intent (a legible, load-bearing error instead of a confusing
        # provider-side one) is preserved there. What this closes is the GAP
        # #3368 never addressed: NOTHING previously warned that ``llm.model``
        # (or a ``model_class_by_purpose`` entry) was already broken, at the
        # one point reyn actually knows it — construction. A typo'd
        # ``llm.model`` used to surface only via whatever code path first
        # happened to call ``resolve()`` on it (#4573's own observed
        # symptom — a lazy, incidental turn-budget-estimation call, NOT a
        # real LLM call — crashed the whole session before the operator
        # ever got a chance to see why or run ``/model`` to fix it). This
        # warning fires unconditionally at construction, so the operator
        # sees the actionable message on the FIRST render, not buried
        # behind whichever internal call site happens to touch the value
        # first (see this module's own docstring's "not applied" phrasing
        # convention, mirrored in the message below).
        _class_position_values = {
            "default_class": default_class,
            **{
                f"model_class_by_purpose.{purpose}": cls
                for purpose, cls in self._purpose_classes.items()
            },
        }
        for _field_label, _value in _class_position_values.items():
            if "/" in _value or _value in self._resolved:
                continue
            import logging

            logging.getLogger(__name__).warning(
                "llm.%s %r does not match any declared model class (%s) and "
                "is not a literal provider/model string — this model is "
                "NOT APPLIED until fixed: any use of it will fail with a "
                "load-bearing error (not silently fall back). Check "
                "reyn.yaml/reyn.local.yaml's `llm.models:` section for a "
                "typo or a load failure, or run `/model <class>` to switch "
                "to a working one for this session.",
                _field_label, _value, ", ".join(sorted(self._resolved)) or "none",
            )

    def resolve(self, name: str) -> ModelSpec:
        """Return the ModelSpec for *name*.

        #4349 (the resolve() rewrite): *name* arrives in one of two
        positions, and this function now tells them apart the same way the
        rest of this module already does for str-form namespace VALUES
        (``_resolve_entry``'s own ``"/" in value`` disambiguation) — no new
        concept, the same heuristic applied at the entry point:

          - contains ``/`` -> a NAME position: a literal LiteLLM model
            string (e.g. an explicit ``--model openai/gpt-4o``, or a
            resolved entry's own ``model`` value re-resolved). Passthrough,
            unchanged from before.
          - no ``/`` and not a known class -> a CLASS position that failed
            to resolve: a typo, or a config whose ``llm.models:`` section
            didn't load. This used to fall into the SAME passthrough as the
            name-position case above — indistinguishable until litellm
            itself rejected the raw class name much later with a confusing
            provider-side error (#3368). Now raises immediately, here, with
            the unresolved name and the known-classes list in the message —
            the same information the removed warning carried, but as the
            legible, load-bearing error the class position always should
            have gotten (``resolve_class_or_fallback`` already treats a
            class-typed position as closed-world for op/phase-supplied
            selections; this closes the SAME gap for config-declared ones).
        """
        if name in self._resolved:
            return self._resolved[name]
        if "/" in name:
            return ModelSpec(model=name, kwargs={})
        raise ValueError(
            f"model class {name!r} not found among known classes "
            f"({', '.join(sorted(self._resolved)) or 'none'}). Check reyn.yaml/"
            "reyn.local.yaml's `llm.models:` section for a typo or a load "
            "failure — or, if this was meant to be a literal LiteLLM model "
            "string, it must include a provider prefix (e.g. 'openai/gpt-4o')."
        )

    def class_ceiling(self) -> "str | None":
        """#4206 T1: the project-declared ``model_max_class`` ceiling, or
        ``None`` when unbounded. Read by a caller (RouterLoop) that already
        knows the resolved class it's about to use, and passes both to
        ``recorded_acompletion`` for enforcement — this accessor does not
        itself enforce anything.
        """
        return self._model_max_class

    def max_input_token_overrides(self) -> "dict[str, int]":
        """#4689: ``{resolved LiteLLM model string: declared max_input_tokens}``
        for every class in this resolver's namespace that set one — the
        bridge from "operator declares a CEILING per model CLASS"
        (``llm.models.<tier>.max_input_tokens``) to
        ``reyn.llm.model_budget.get_max_input_tokens``, which only ever
        sees the already-resolved model STRING, at 8 call sites that have
        no access to a class name or this resolver. The caller (wherever a
        ``ModelResolver`` is constructed — ``registry_bootstrap.py`` and 3
        other sites) registers this dict once via
        ``model_budget.register_max_input_overrides``; ``_resolve_max_input``
        then consults that registration by MODEL STRING, so all 8 call
        sites benefit without any of them changing.

        Every namespace entry is already resolved (``__init__``'s own
        "resolve all entries at startup, fail-fast" pass), so this is a
        pure read — no re-resolution, no I/O.

        Raises ``ValueError`` if two classes in THIS resolver's own
        namespace resolve to the SAME model string with DIFFERENT declared
        ``max_input_tokens`` — ambiguous (which one wins?), caught here
        before it ever reaches the process-shared registration step
        (which does its OWN, separate collision check across resolvers —
        see that function's docstring for why both checks exist)."""
        result: "dict[str, int]" = {}
        for name, spec in self._resolved.items():
            if spec.max_input_tokens is None:
                continue
            existing = result.get(spec.model)
            if existing is not None and existing != spec.max_input_tokens:
                raise ValueError(
                    f"conflicting max_input_tokens for model {spec.model!r}: "
                    f"multiple classes resolve to this same model string "
                    f"with different declared values ({existing} vs "
                    f"{spec.max_input_tokens}, class {name!r}) — which one "
                    f"should win is ambiguous; give the model classes "
                    f"agreeing max_input_tokens values, or point them at "
                    f"different model strings."
                )
            result[spec.model] = spec.max_input_tokens
        return result

    def class_for_purpose(self, purpose: str) -> str:
        """#1672: the model CLASS for a logical call *purpose* (router / control_ir
        / tool / judge — NOT compaction, #3785: compaction always follows
        ``ReynConfig.model``/``Session.model`` directly, never this map).

        A per-purpose override (``model_class_by_purpose``) wins; otherwise the
        configured default class (``ReynConfig.model``). This is the OS-side mirror
        of ``ReynConfig.model_class_for`` for the many call sites that hold a
        threaded ``ModelResolver`` but not the ``ReynConfig`` object. Explicit
        per-call selections (op.model / frontmatter) are applied by the caller
        BEFORE this fallback and still win. Returns a CLASS name — feed it through
        ``resolve()`` to get the ModelSpec / litellm string.
        """
        return self.purpose_class_or(purpose, self._default_class)

    def purpose_class_or(self, purpose: str, default: str) -> str:
        """#1679: the per-purpose class override if one is configured, else
        *default* — a caller-supplied fallback.

        Unlike ``class_for_purpose`` (whose fallback is the resolver's configured
        ``default_class`` = ``ReynConfig.model``), this lets a caller keep its OWN
        existing model source when no override is set — needed wherever that
        source can diverge from ``default_class`` under a per-run model
        override, so feeding ``default_class`` would silently move the call
        OFF the agent's chosen model.

        #3785: this is NO LONGER how compaction resolves its model (removed
        the ``model_class_by_purpose.compaction`` override #1679 introduced
        here as the motivating example — compaction never tracked a
        ``/model`` switch under that wiring, so an operator-set override was
        silently getting a STALE model, not the one it named; see
        ``Session._rebuild_derived_model_engines_for_model``). Still used for
        the OTHER purposes whose site-fallback can diverge from
        ``default_class`` the same way. Returns a CLASS name — feed it
        through ``resolve()`` to get the litellm string.
        """
        return self._purpose_classes.get(purpose, default)

    def is_known_class(self, name: str) -> bool:
        """Return True if name is a configured model class (i.e. present in the namespace).

        Use this to distinguish intentional model-class overrides (e.g. "standard",
        "light") from literal LiteLLM strings that may have been injected by the LLM
        and are not in the proxy configuration.
        This prevents LLM-hallucinated model strings from bypassing the proxy.
        """
        return name in self._resolved

    def known_classes(self) -> list[str]:
        """Return a sorted list of all known model class names (user-configured +
        built-ins). Use for user-facing display (e.g. ``/model`` no-arg) and for
        validation error messages — single public surface, avoids callers reaching
        into ``_resolved`` directly."""
        return sorted(self._resolved)

    def resolve_class_or_fallback(
        self, requested: str | None, fallback: str | None, *, where: str = "",
    ) -> str:
        """Resolve a CLASS-TYPED model selection (op- or phase-supplied) closed-world.

        #1454 PR-B (the unified class/name rule): a class-typed position is
        closed-world — a ``requested`` value that is NOT a known class is NOT
        passed through as a literal LiteLLM model (that would let an
        LLM-injected string bypass the proxy config, which is
        the single source of truth for model selection). Returns ``requested``
        only when it is a known class; otherwise logs ONE decision-enabling
        warning and returns the trusted ``fallback``.

        This is the "standard gate" promotion of :meth:`is_known_class` — every
        op- or phase-supplied model field (``op.model``) routes through here rather
        than each call site re-implementing the guard. Operator-config model
        references (``cfg.llm.model`` etc., from reyn.yaml — #4174 T3: moved
        from top-level ``cfg.model``) deliberately do NOT use this gate:
        literal LiteLLM strings there are an intentional backward-compat
        passthrough (see :meth:`resolve`).
        """
        if requested and not self.is_known_class(requested):
            import logging

            logging.getLogger(__name__).warning(
                "%s: model %r is not a known model class — ignoring and using "
                "%r instead. Use a model class (e.g. light / standard / strong, "
                "or one defined in reyn.yaml llm.models:) so the proxy config stays "
                "the single source of truth.",
                where or "model selection", requested, fallback or "standard",
            )
            return fallback or "standard"
        return requested or fallback or "standard"

    # ------------------------------------------------------------------
    # Internal resolution helpers
    # ------------------------------------------------------------------

    def _resolve_entry(
        self, name: str, value: Any, seen: frozenset[str]
    ) -> ModelSpec:
        """Resolve a single namespace entry, following extends chains.

        Args:
            name:   The class name being resolved (used in cycle / error messages).
            value:  The raw value from the namespace (str or dict).
            seen:   Names already on the current resolution stack (cycle detection).

        Returns:
            Fully resolved ModelSpec.

        Raises:
            ValueError: cycle detected, unknown reference, or missing ``model`` field.
        """
        if name in seen:
            chain = " -> ".join(list(seen) + [name])
            raise ValueError(f"circular extends detected: {chain}")

        seen = seen | {name}

        if isinstance(value, str):
            if "/" in value:
                # Literal LiteLLM model string (backward compat).
                return ModelSpec(model=value, kwargs={})
            else:
                # Class reference shorthand: treat as {extends: value}.
                return self._resolve_reference(value, seen)

        if isinstance(value, dict):
            extends_target = value.get("extends")
            if extends_target is not None:
                # Resolve base, then deep-merge override on top.
                base_spec = self._resolve_reference(str(extends_target), seen)
                override = _strip_keys(value, {"extends", "model"})
                base_dict = _spec_to_dict(base_spec)
                merged = _deep_merge(base_dict, override)
                # dict form may also override the model string explicitly.
                if "model" in value:
                    merged["model"] = value["model"]
                if "model" not in merged:
                    raise ValueError(
                        f"ModelSpec for '{name}' is missing a 'model' field "
                        f"after resolving extends chain"
                    )
                # #4689: reuse from_config's own field extraction, rather
                # than constructing ModelSpec(model=model, kwargs=merged)
                # directly — the direct-construction form left
                # api_base/provider/stream (and now max_input_tokens) in
                # kwargs instead of extracting them as their own fields
                # (a pre-existing gap on the extends-merge path specifically:
                # _spec_to_dict already carries them into `merged` via
                # `base_dict`, but nothing pulled them back OUT before this
                # constructor call). Routing through from_config closes
                # that for all four fields at once, in the same PR that
                # would otherwise have left max_input_tokens as the sole
                # newly-added field with the same bug the other three
                # already had.
                return ModelSpec.from_config(merged)
            else:
                # Plain dict form (no extends): use from_config.
                return ModelSpec.from_config(value)

        raise ValueError(
            f"Namespace entry '{name}' must be str or dict, "
            f"got {type(value).__name__}"
        )

    def _resolve_reference(self, target: str, seen: frozenset[str]) -> ModelSpec:
        """Look up *target* in the namespace and resolve it.

        Raises:
            ValueError: if *target* is not in the namespace.
        """
        if target not in self._namespace:
            raise ValueError(
                f"Unknown model class reference '{target}'. "
                f"Available classes: {sorted(self._namespace.keys())}"
            )
        # If already cached, return directly (avoids redundant recursion for shared bases).
        if target in self._resolved:
            return self._resolved[target]
        resolved = self._resolve_entry(target, self._namespace[target], seen)
        # Cache mid-resolution to avoid double-resolving shared bases.
        self._resolved[target] = resolved
        return resolved
