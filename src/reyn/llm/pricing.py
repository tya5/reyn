"""
Token usage tracking and cost estimation.

Cost calculation delegates entirely to litellm's built-in pricing database —
no pricing table is maintained here. litellm is updated with each release
to reflect current provider prices.

The pricing info litellm uses is snapshotted at run time and stored in the
eval result JSON so past runs can be audited accurately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UsageSource(str, Enum):
    """Where a ``TokenUsage``'s numbers came from (#3351).

    A token count is not self-describing: a provider-reported figure and a
    LOCAL ESTIMATE computed by ``litellm.token_counter`` are the same int,
    and both flow into ``record_llm`` → ``/cost`` → the budget caps. The
    estimate fires whenever a streamed response carries no usage payload
    (``litellm``'s ``stream_chunk_builder`` fills the gap with
    ``prompt_tokens or token_counter(...)``); measured on a live Gemini call
    it recorded 13 against a provider truth of 7 (+86%). The estimate is
    deliberately KEPT — a number beats no number — but its ORIGIN must not
    disappear, because a cap that stopped a run, or a `/cost` figure being
    audited, is only interpretable if you can tell which of the two you are
    looking at, and cost auditing happens after the fact.

    ``UNKNOWN`` is the default, and that direction is the whole point: a
    producer that forgets to state provenance yields "unknown", never a
    falsely confident "provider". Same reasoning as ``None``-never-``0`` for
    a per-turn figure (#3342/#3347) — the silent value must be the one that
    cannot be mistaken for a fact.
    """

    #: The provider's own usage payload reported these counts.
    PROVIDER = "provider"
    #: At least one of prompt/completion was filled locally by
    #: ``litellm.token_counter`` because the provider reported none.
    ESTIMATED = "estimated"
    #: Provenance was never stated — e.g. a ledger record written before
    #: #3351, or a usage object built outside the LLM chokepoint. NOT a
    #: claim that the number is exact.
    UNKNOWN = "unknown"


#: Least-confident-wins ordering for merging two sources when usage objects are
#: summed (``__add__`` / ``__iadd__``). An aggregate is only ``PROVIDER`` when
#: EVERY contribution was; one estimated call contaminates the total, which is
#: exactly what an auditor of that total needs to know. ``ESTIMATED`` outranks
#: ``UNKNOWN`` as the loudest answer: "known to be approximate" is more
#: actionable than "not stated", and an aggregate that contains an estimate must
#: say so rather than hide behind "unknown".
_SOURCE_CONFIDENCE: dict[UsageSource, int] = {
    UsageSource.PROVIDER: 2,
    UsageSource.UNKNOWN: 1,
    UsageSource.ESTIMATED: 0,
}


def merge_usage_sources(*sources: UsageSource) -> UsageSource:
    """Provenance of a figure summed from parts whose sources are *sources*.

    Least-confident wins (:data:`_SOURCE_CONFIDENCE`) — the ONE declaration of
    that rule, used both by ``TokenUsage`` arithmetic and by ``BudgetTracker``'s
    per-turn provenance bucket, so a turn total and a summed ``TokenUsage``
    cannot disagree about what kind of number they are. No arguments →
    ``UNKNOWN`` (nothing was stated).
    """
    if not sources:
        return UsageSource.UNKNOWN
    return min(sources, key=lambda s: _SOURCE_CONFIDENCE[s])


def parse_usage_source(raw: object) -> UsageSource:
    """Read a serialized provenance value back (#3351).

    Anything this function cannot positively recognise — a missing field (every
    ledger record and usage dict written before #3351), a null, a typo, a
    future variant this build does not know — becomes ``UNKNOWN``. It never
    becomes ``PROVIDER``: an unreadable origin is exactly the case that must
    not read as provider-verified, and that is what makes old records still
    parse without quietly gaining a claim they never made.
    """
    if isinstance(raw, UsageSource):
        return raw
    if isinstance(raw, str):
        try:
            return UsageSource(raw)
        except ValueError:
            return UsageSource.UNKNOWN
    return UsageSource.UNKNOWN


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Prompt-cache metrics (#1772). Both are SUBSETS / side-metrics of the
    # billed tokens, NOT additive to total_tokens:
    #   cached_tokens          — prompt tokens served from cache (cache READ /
    #                            hit). A subset of prompt_tokens; cross-provider
    #                            normalized (litellm surfaces this as both
    #                            usage.cache_read_input_tokens and
    #                            usage.prompt_tokens_details.cached_tokens).
    #   cache_creation_tokens  — tokens written to the cache (Anthropic
    #                            cache-write); 0 for providers without an
    #                            explicit write metric (OpenAI / Gemini).
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    #: #3351: PROVENANCE of the counts above, carried BY the same object that
    #: carries them — so no consumer can hold the number without also holding
    #: its origin. Defaults to ``UNKNOWN`` (never ``PROVIDER``): a forgotten
    #: producer reads as "not stated", not as a provider-verified fact.
    source: UsageSource = field(default=UsageSource.UNKNOWN)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def _merged_source(self, other: "TokenUsage") -> UsageSource:
        """#3351: the provenance of ``self + other`` — least-confident wins.

        An operand that carries NO tokens at all is provenance-neutral: an
        empty accumulator (``TokenUsage()``, the shape every aggregation in
        ``registry`` / ``router_loop`` / ``budget_gateway`` starts from) states
        nothing about numbers it does not have, and must not drag a run of
        provider-verified calls down to ``UNKNOWN``.

        ★The assumption that rule rests on: **a zero-token operand is treated as
        stating no information.** True of the empty accumulators above — but if
        a producer ever emits zero as the RESULT OF FAILING TO COUNT, this rule
        silently raises the aggregate's confidence:
        ``ESTIMATED(0, 0) + PROVIDER(100, 20)`` reports ``PROVIDER`` and the
        estimated marking disappears. Latent, not live, at the time of writing:
        litellm's ``token_counter failed → prompt_tokens = 0`` fallback sits in
        the text-completion branch, ``completion_tokens`` is computed
        separately, and reyn's chat path records nothing at all when a response
        carries no usage — so an all-zero-but-meant-something usage has no
        production producer. It is written down because THIS module exists to
        close a "silently raises confidence" path, and the one place such a
        shape would be least expected is inside its own merge rule. A future
        producer of count-failure zeros must make that state explicit (its own
        source, or a not-a-number sentinel) rather than inherit neutrality here.
        """
        if self.total_tokens == 0 and self.cached_tokens == 0 and self.cache_creation_tokens == 0:
            return other.source
        if other.total_tokens == 0 and other.cached_tokens == 0 and other.cache_creation_tokens == 0:
            return self.source
        return merge_usage_sources(self.source, other.source)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            source=self._merged_source(other),
        )

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        # Merge BEFORE mutating the counts — the neutrality rule reads both
        # operands' token totals, and self's are about to change.
        merged = self._merged_source(other)
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.source = merged
        return self

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            # #3351: provenance travels with the numbers through every
            # serialization boundary too, so a round-tripped usage cannot
            # come back looking provider-verified.
            "usage_source": self.source.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        """Reconstruct from a dict produced by ``to_dict``.

        Resilient to missing fields AND to a malformed value (defaults to 0) —
        useful for backward-compat reading of older / hand-edited / corrupted
        WAL records. ``.get(key, 0)`` only defaults a *missing* key, so a key
        present with ``null`` or a non-numeric value would otherwise crash
        (``int(None)`` → TypeError, ``int("abc")`` → ValueError); ``_coerce_int``
        closes that gap to honour the stated resilience contract.
        """
        def _coerce_int(v: object) -> int:
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        return cls(
            prompt_tokens=_coerce_int(data.get("prompt_tokens", 0)),
            completion_tokens=_coerce_int(data.get("completion_tokens", 0)),
            cached_tokens=_coerce_int(data.get("cached_tokens", 0)),
            cache_creation_tokens=_coerce_int(data.get("cache_creation_tokens", 0)),
            source=parse_usage_source(data.get("usage_source")),
        )


def _usage_object_for(litellm_module: "Any", usage: TokenUsage):
    """Build a ``litellm.types.utils.Usage`` carrying the cache breakdown so
    ``litellm.cost_per_token`` prices cached tokens at the cache rate instead
    of the full input rate.

    #4395: takes the ALREADY-confirmed litellm module (``ensure_litellm_
    ready()``'s own return value) as a parameter and reads ``Usage`` off it
    via attribute access, rather than this function doing its own separate
    ``from litellm.types.utils import Usage`` statement — lead-coder found
    (with a live measurement) that THAT statement, on its own, in a process
    where litellm was never successfully imported before, triggers the full
    parent-package import AND litellm's ``default_encoding`` module-level
    tiktoken sync fetch — the exact #4395 hazard, in a form (``from litellm.
    X import Y``, not ``import litellm`` / ``from litellm import``) neither
    this PR's nor architect's own site census had grepped for. Reading
    ``litellm_module.types.utils.Usage`` off the confirmed module instead
    means this function can no longer independently touch litellm at all —
    it is structurally incapable of bypassing the chokepoint, not just
    reliant on its current caller happening to gate it first (verified
    empirically: ``litellm.types.utils`` is already a populated attribute
    on the module object after a plain successful ``import litellm`` — no
    separate submodule import statement is needed).

    Provider-convention note: reyn's ``TokenUsage.prompt_tokens`` ALREADY
    INCLUDES ``cached_tokens`` and ``cache_creation_tokens`` as subsets — this
    is litellm's own normalized ``response.usage.prompt_tokens`` value (see
    ``_extract_usage`` in ``llm.py``; litellm's Anthropic transformation adds
    both cache figures into ``prompt_tokens`` before returning the response
    object, matching the OpenAI convention where ``prompt_tokens_details.
    cached_tokens`` is a subset of ``prompt_tokens``). Passing the breakdown
    via ``prompt_tokens_details`` (OpenAI-style) rather than the top-level
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` kwargs is
    deliberate: litellm's ``cost_per_token`` treats the latter as "Anthropic
    style" (prompt_tokens EXCLUDES cache) and RE-ADDS them to prompt_tokens
    before costing — which would double-count here, since our prompt_tokens
    already includes them. The ``prompt_tokens_details`` path takes
    prompt_tokens at face value and subtracts cache tokens internally
    (``text_tokens = prompt_tokens - cache_hit - cache_creation``), which is
    the correct convention for reyn's already-normalized TokenUsage.
    """
    return litellm_module.types.utils.Usage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        prompt_tokens_details={
            "cached_tokens": usage.cached_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
        },
    )


def estimate_cost(
    model: str,
    usage: TokenUsage,
) -> tuple[float | None, dict | None]:
    """
    Estimate cost in USD using litellm's pricing database — cache-aware
    (#cache-cost-accuracy): cached (cache-read) prompt tokens are priced at
    the model's discounted cache-read rate and cache-creation (cache-write)
    tokens at the cache-creation rate, not the full input rate, by passing
    the cache breakdown to ``litellm.cost_per_token`` (see
    ``_usage_object_for`` for the provider-convention this relies on).

    Returns (cost_usd, pricing_snapshot).
    pricing_snapshot records the per-token prices litellm used at this moment
    — store it in run results so past runs remain auditable after price changes.
    Returns (None, None) if litellm does not know the model.
    """
    if usage.total_tokens == 0:
        return 0.0, None

    try:
        # #4395 PR-1: route through the sole chokepoint (this bare `import
        # litellm` never did before) so a failure isn't independently
        # re-attempted — it was previously bypassing ensure_litellm_ready()
        # entirely, meaning EVERY cost-estimation call (which runs every
        # turn, unlike model_budget's occasional lookups) re-attempted
        # litellm's own slow, unbounded import on every failure. `None`
        # here is already this function's own documented "cost unknown"
        # sentinel (see the docstring above) — no new fallback shape.
        # #4395 (seam alignment, owner directive): reads the module off
        # `ensure_litellm_ready()`'s own return value rather than a
        # separate `import litellm` statement — the ONLY form the
        # planned litellm-import gate (#4421) will allow outside
        # `litellm_bootstrap.py` itself.
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready
        litellm = ensure_litellm_ready()
        if litellm is None:
            return None, None

        # #1829 S2 (option 3): a ``litellm.model_cost`` entry can EXIST with None
        # prices — a price-less PLACEHOLDER (e.g. a ``litellm.Router`` deployment
        # registration adds one). ``cost_per_token`` then returns 0.0 for it,
        # conflating "cost unknown" with "free". An unpriced model means cost
        # UNKNOWN → return (None, None), not 0.0. (unknown ≠ free — None is the
        # explicit unknown sentinel; 0.0 would wrongly read as a real free call.)
        _entry = litellm.model_cost.get(model)
        if (not _entry
                or _entry.get("input_cost_per_token") is None
                or _entry.get("output_cost_per_token") is None):
            return None, None

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            usage_object=_usage_object_for(litellm, usage),
        )
        total_cost = prompt_cost + completion_cost

        # Snapshot the per-token prices litellm has for this model
        entry = litellm.model_cost.get(model, {})
        input_per_token = entry.get("input_cost_per_token")
        output_per_token = entry.get("output_cost_per_token")

        try:
            from importlib.metadata import version as _pkg_version
            litellm_version = _pkg_version("litellm")
        except Exception:
            litellm_version = "unknown"

        snapshot = {
            "model": model,
            "prompt_per_1m_usd": round(input_per_token * 1_000_000, 6) if input_per_token else None,
            "completion_per_1m_usd": round(output_per_token * 1_000_000, 6) if output_per_token else None,
            "source": "litellm",
            "litellm_version": litellm_version,
        }
        return total_cost, snapshot

    except Exception:
        return None, None


@dataclass
class CostBreakdown:
    """Cache-aware cost breakdown for one LLM call (or an aggregation across
    many, via ``__add__`` / ``__iadd__``) — the 4 components the cost panel
    is expected to render (#cache-cost-accuracy), plus the SAVINGS figures the
    owner flagged as first-class for the panel's savings block.

    ``prompt_cost`` covers only the NON-cached portion of the prompt (see
    ``estimate_cost_breakdown`` for the exact token split). ``total_cost`` is
    the sum of all 4 cost components — decisively pinned equal to
    ``estimate_cost``'s litellm-derived total by the accuracy test
    (components-sum-to-total invariant).

    Savings surface (backend-only; no panel UI yet):
      - ``cache_savings`` — how many USD the cached (cache-read) tokens saved
        vs paying the full input rate for them:
        ``cached_tokens × (input_rate − cache_read_rate)``. A dollar amount,
        so it aggregates additively like the cost components.
      - ``cache_hit_rate`` — token-level cache hit rate,
        ``cached_tokens / prompt_tokens`` (0.0 when ``prompt_tokens`` is 0 —
        divide-by-zero guarded). Derived from the aggregatable
        ``prompt_tokens`` / ``cached_tokens`` counters so the ratio stays
        correct across a multi-turn aggregation (summing per-turn ratios
        would be wrong — summing the underlying token counts is not).
    """
    prompt_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_creation_cost: float = 0.0
    completion_cost: float = 0.0
    # Savings block inputs (aggregatable). ``cache_savings`` is a USD amount;
    # ``prompt_tokens`` / ``cached_tokens`` back the derived ``cache_hit_rate``.
    cache_savings: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_cost(self) -> float:
        return (
            self.prompt_cost
            + self.cache_read_cost
            + self.cache_creation_cost
            + self.completion_cost
        )

    @property
    def cache_hit_rate(self) -> float:
        """Token-level cache hit rate ``cached_tokens / prompt_tokens``; 0.0
        when ``prompt_tokens`` is 0 (divide-by-zero guard)."""
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            prompt_cost=self.prompt_cost + other.prompt_cost,
            cache_read_cost=self.cache_read_cost + other.cache_read_cost,
            cache_creation_cost=self.cache_creation_cost + other.cache_creation_cost,
            completion_cost=self.completion_cost + other.completion_cost,
            cache_savings=self.cache_savings + other.cache_savings,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    def __iadd__(self, other: "CostBreakdown") -> "CostBreakdown":
        self.prompt_cost += other.prompt_cost
        self.cache_read_cost += other.cache_read_cost
        self.cache_creation_cost += other.cache_creation_cost
        self.completion_cost += other.completion_cost
        self.cache_savings += other.cache_savings
        self.prompt_tokens += other.prompt_tokens
        self.cached_tokens += other.cached_tokens
        return self

    def to_dict(self) -> dict:
        return {
            "prompt_cost": self.prompt_cost,
            "cache_read_cost": self.cache_read_cost,
            "cache_creation_cost": self.cache_creation_cost,
            "completion_cost": self.completion_cost,
            "total_cost": self.total_cost,
            "cache_savings": self.cache_savings,
            "cache_hit_rate": self.cache_hit_rate,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CostBreakdown":
        """Reconstruct from a dict produced by :meth:`to_dict` — #5771
        stage②: the AG-UI wire's own decode side for ``cost_breakdown_
        session``/``cost_breakdown_agent``/``cost_breakdown_project``
        (``project_remote_snapshot``, read_model.py), reusing this ONE
        encode/decode pair rather than inventing a second serialization
        (architect's own explicit instruction on #5771).

        ``total_cost``/``cache_hit_rate`` are DERIVED properties, not
        constructor fields — :meth:`to_dict` includes them for a reader
        that only wants the finished figures (the cost panel's own
        table), but reconstructing from them here would either silently
        drop them (they're not settable) or, worse, invite a future
        edit to add them as fields and diverge from the property that
        already computes them. Only the 7 real fields are read back;
        the properties re-derive correctly from those alone.

        Resilient to missing/malformed fields the same way ``TokenUsage.
        from_dict`` is (older/hand-edited/corrupted data defaults each
        field to its own dataclass default, never raises)."""
        def _coerce_float(v: object, default: float) -> float:
            try:
                return float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        def _coerce_int(v: object, default: int) -> int:
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        return cls(
            prompt_cost=_coerce_float(data.get("prompt_cost"), 0.0),
            cache_read_cost=_coerce_float(data.get("cache_read_cost"), 0.0),
            cache_creation_cost=_coerce_float(data.get("cache_creation_cost"), 0.0),
            completion_cost=_coerce_float(data.get("completion_cost"), 0.0),
            cache_savings=_coerce_float(data.get("cache_savings"), 0.0),
            prompt_tokens=_coerce_int(data.get("prompt_tokens"), 0),
            cached_tokens=_coerce_int(data.get("cached_tokens"), 0),
        )


def estimate_cost_breakdown(
    model: str,
    usage: TokenUsage,
) -> "CostBreakdown | None":
    """Cache-aware cost BREAKDOWN into 4 components: prompt (non-cached input)
    / cache-read (cache-hit discount) / cache-creation (cache-write surcharge)
    / completion (output), plus the savings figures (``cache_savings`` USD +
    ``cache_hit_rate``) for the panel's savings block. Data-only — no
    rendering; the cost panel reads this to display a breakdown instead of
    just the total.

    Token-split convention (mirrors litellm's own ``generic_cost_per_token``,
    verified empirically — see ``_usage_object_for``'s docstring for why):
    ``TokenUsage.prompt_tokens`` already INCLUDES ``cached_tokens`` and
    ``cache_creation_tokens`` as subsets, so the non-cached ("regular")
    portion priced at the full input rate is
    ``prompt_tokens - cached_tokens - cache_creation_tokens`` (floored at 0).

    Cache-rate fallback: when a model has no explicit
    ``cache_read_input_token_cost`` / ``cache_creation_input_token_cost``
    entry, litellm itself defaults that rate to ``0.0`` (NOT the input rate —
    verified against ``litellm.litellm_core_utils.llm_cost_calc.utils.
    _get_cost_per_unit``'s ``default_value=0.0``); this function does the same
    so the components-sum-to-total invariant holds against litellm's own
    total for every model, cache-capable or not.

    Note: does not replicate litellm's >200k-token TIERED pricing threshold
    (rare for reyn's usage) — the accuracy test stays below that threshold.

    Returns ``None`` for an unpriced/unknown model (mirrors ``estimate_cost``'s
    None-sentinel — unknown ≠ free, #1829).
    """
    if usage.total_tokens == 0:
        return CostBreakdown()

    try:
        # #4395 PR-1: route through the sole chokepoint — see estimate_cost's
        # identical fix above for the full reasoning (this bare `import
        # litellm` never went through ensure_litellm_ready() before,
        # independently re-attempting litellm's own slow import on every
        # failed cost-breakdown call). `None` is already this function's
        # own documented "unpriced/unknown" sentinel. #4395 (seam
        # alignment): reads the module off the return value — see
        # estimate_cost's identical fix above.
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready
        litellm = ensure_litellm_ready()
        if litellm is None:
            return None

        entry = litellm.model_cost.get(model)
        if (not entry
                or entry.get("input_cost_per_token") is None
                or entry.get("output_cost_per_token") is None):
            return None

        input_rate = float(entry["input_cost_per_token"])
        output_rate = float(entry["output_cost_per_token"])
        cache_read_rate = float(entry.get("cache_read_input_token_cost") or 0.0)
        cache_creation_rate = float(entry.get("cache_creation_input_token_cost") or 0.0)

        regular_prompt_tokens = max(
            usage.prompt_tokens - usage.cached_tokens - usage.cache_creation_tokens,
            0,
        )

        # Savings: what the cached tokens would have cost at the full input
        # rate MINUS what they actually cost at the cache-read rate.
        cache_savings = usage.cached_tokens * (input_rate - cache_read_rate)

        return CostBreakdown(
            prompt_cost=regular_prompt_tokens * input_rate,
            cache_read_cost=usage.cached_tokens * cache_read_rate,
            cache_creation_cost=usage.cache_creation_tokens * cache_creation_rate,
            completion_cost=usage.completion_tokens * output_rate,
            cache_savings=cache_savings,
            prompt_tokens=usage.prompt_tokens,
            cached_tokens=usage.cached_tokens,
        )
    except Exception:
        return None


# ── FP-0063 PC: embedding cost (INDEPENDENT of the chat CostBreakdown above) ──


def estimate_embedding_cost(
    model: str,
    total_tokens: int,
) -> tuple[float | None, dict | None]:
    """Estimate one embedding call's cost in USD, extending the SAME
    ``litellm.model_cost`` lookup ``estimate_cost``/``estimate_cost_breakdown``
    use above to embedding-mode entries (~124 observed in the vendored litellm
    at proposal time, e.g. ``amazon.titan-embed-text-v2:0``) — not a new /
    parallel rate table (FP-0063 X4).

    An embedding call is INPUT-ONLY: there is no completion, no cache read/
    write, so this prices ``total_tokens`` directly at the model's
    ``input_cost_per_token`` rate rather than going through
    ``litellm.cost_per_token``'s completion/cache-aware usage-object plumbing
    (which ``estimate_cost`` needs and this does not). Embedding-mode entries
    carry ``output_cost_per_token: 0.0`` (never ``None``, verified empirically
    against the installed litellm's ``model_cost`` table), so only
    ``input_cost_per_token`` is required to price a call.

    Returns ``(cost_usd, pricing_snapshot)``. Mirrors ``estimate_cost``'s
    unknown-model sentinel: an unpriced/unknown model returns ``(None, None)``
    — unknown != free (#1829) — so a model litellm cannot price degrades
    VISIBLY (the caller can detect "not priced" and surface it) rather than
    silently reading as ``$0.00``, which would recreate the exact invisibility
    bug this feature exists to close.
    """
    if total_tokens <= 0:
        return 0.0, None

    try:
        # #4395 PR-1: route through the sole chokepoint — same fix and
        # reasoning as estimate_cost/estimate_cost_breakdown above (this
        # bare `import litellm` never went through ensure_litellm_ready()
        # either — a 4th site, beyond the 3 originally measured for this
        # file, found while fixing the other 3). `None` is already this
        # function's own documented "unpriced/unknown" sentinel. #4395
        # (seam alignment): reads the module off the return value — see
        # estimate_cost's identical fix above.
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready
        litellm = ensure_litellm_ready()
        if litellm is None:
            return None, None

        entry = litellm.model_cost.get(model)
        if not entry or entry.get("input_cost_per_token") is None:
            return None, None

        input_rate = float(entry["input_cost_per_token"])
        cost_usd = total_tokens * input_rate

        try:
            from importlib.metadata import version as _pkg_version
            litellm_version = _pkg_version("litellm")
        except Exception:
            litellm_version = "unknown"

        snapshot = {
            "model": model,
            "input_per_1m_usd": round(input_rate * 1_000_000, 6),
            "source": "litellm",
            "litellm_version": litellm_version,
        }
        return cost_usd, snapshot
    except Exception:
        return None, None


@dataclass
class EmbeddingCost:
    """Independent embedding-spend aggregate (FP-0063 PC) — deliberately NOT
    a ``CostBreakdown`` field / component. Owner decision (2026-07-15,
    proposal 0063 "Embedding cost is tracked INDEPENDENTLY"): an embedding
    call is input-only and structurally uncacheable, so folding it into
    ``CostBreakdown.prompt_cost`` would dilute ``cache_hit_rate`` /
    ``cache_savings`` with tokens that could never have been cached — those
    are chat-call figures and must stay chat-only. This is the embedding
    aggregate's own, separate, additive total.

    Mixed-model correctness (X6): each call is priced at ITS OWN model's rate
    (``estimate_embedding_cost`` is called once per call, with that call's
    model) BEFORE being folded in here via ``__add__``/``__iadd__`` — dollars
    aggregate additively across models; tokens are never pooled across models
    and priced afterwards at a single rate.

    ``unpriced_calls`` is the visibility mechanism for an unknown model
    (``estimate_embedding_cost`` returning ``None``): such a call still counts
    toward ``tokens``/``calls`` but contributes 0 to ``cost_usd`` while
    incrementing this counter — so "some spend is not reflected in cost_usd"
    stays observable instead of silently reading as a real $0.00 call.
    """

    cost_usd: float = 0.0
    tokens: int = 0
    calls: int = 0
    unpriced_calls: int = 0

    def __add__(self, other: "EmbeddingCost") -> "EmbeddingCost":
        return EmbeddingCost(
            cost_usd=self.cost_usd + other.cost_usd,
            tokens=self.tokens + other.tokens,
            calls=self.calls + other.calls,
            unpriced_calls=self.unpriced_calls + other.unpriced_calls,
        )

    def __iadd__(self, other: "EmbeddingCost") -> "EmbeddingCost":
        self.cost_usd += other.cost_usd
        self.tokens += other.tokens
        self.calls += other.calls
        self.unpriced_calls += other.unpriced_calls
        return self

    def to_dict(self) -> dict:
        return {
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "calls": self.calls,
            "unpriced_calls": self.unpriced_calls,
        }
