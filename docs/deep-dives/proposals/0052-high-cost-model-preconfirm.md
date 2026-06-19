# FP-0052 — High-cost model pre-confirmation (#1830)

**Status**: draft  
**Author**: tui-coder  
**Issue**: #1830  
**Scope**: `llm/` utility + config + `/model` slash integration + session startup + TUI surface

---

## Problem

Reyn's budget system is enforcement-after-the-fact: `BudgetTracker.check_pre_llm` refuses
calls that would exceed a hard cap, and `ask_on_exceed` asks the user to extend after
a dimension is exceeded. Neither tells the user *before they choose a model* that the model
is expensive.

Hermes surfaces this via a cost-guard prompt when a high-tier model is selected.
Reyn has all the data needed (`litellm.model_cost` carries `input_cost_per_token` for
2784+ models) but no pre-selection confirmation path.

---

## Non-duplication audit (lead prerequisite)

| Existing mechanism | Axis | Overlap? |
|---|---|---|
| `BudgetTracker.check_pre_llm` + `ask_on_exceed` | Cumulative spend exceeds hard cap | **None** — per-token rate vs. total spend |
| `ContextBudgetAdvisor` / `maybe_force_compact` | Context window token ceiling | **None** — token count, not cost rate |
| `lifecycle_forwarder.on_budget_warn` | Warn after spend threshold crossed | **None** — post-hoc, not pre-selection |
| `embedding/cost_estimator.py` | Job-level cost preflight (embedding only) | Pattern match — same idea, different domain |

Pre-selection model rate warning is **orthogonal** to all existing budget mechanisms.

---

## Seam map

### Seam 1 — Cost-rate lookup utility (new, `llm/model_cost_rate.py`)

```python
def get_input_cost_per_1m_usd(model: str) -> float | None:
    """Return litellm's input_cost_per_token * 1e6, or None if unknown."""
    import litellm
    entry = litellm.model_cost.get(model, {})
    per_token = entry.get("input_cost_per_token")
    return float(per_token) * 1_000_000 if per_token is not None else None

def is_high_cost_model(model: str, threshold_per_1m: float) -> bool:
    cost = get_input_cost_per_1m_usd(model)
    return cost is not None and cost > threshold_per_1m
```

Pure function, no side effects, testable without session.  
Uses the same litellm DB already used by `estimate_cost` in `pricing.py`.

### Seam 2 — Config (`reyn.yaml` → `ReynConfig.cost_warn`)

New `CostWarnConfig` dataclass alongside existing `BudgetConfig`:

```yaml
# reyn.yaml
cost_warn:
  enabled: true                          # default: true; set false to suppress
  model_threshold_per_1m_input_usd: 5.0  # warn if input > $5/1M tokens
```

```python
@dataclass
class CostWarnConfig:
    enabled: bool = True
    model_threshold_per_1m_input_usd: float = 5.0
```

Default threshold: `$5.00/1M input tokens` — above standard Claude Sonnet tier
(`claude-sonnet-4-x` ≈ $3/1M) so it fires for Opus-class and comparable models
but not for typical usage.

Threshold is user-overridable in `reyn.yaml` (not hardcoded — per
`feedback_no_uncustomizable_hardcoded_choices`).

### Seam 3 — Injection points (where the check fires)

Two points; both call `is_high_cost_model(resolved_model, threshold)`:

**A. `/model <class>` switch** (`interfaces/slash/model.py`, after `resolver.is_known_class`):
- Resolve the requested class to the litellm model string
- If high-cost: surface a `model_cost_warn` event (via `session._events`) with
  `{model, cost_per_1m, threshold, action="model_override"}`
- Proceed with the switch regardless — this is a *warn*, not a block
  (blocking is `BudgetTracker`'s job; this is informational)

**B. Session startup** (`session.py`, after initial model is resolved):
- Same check, same event, `action="session_start"`
- Only fires once per session (session-scoped flag `_cost_warned_model: str | None`)

**Event shape** (P6 — every state change emits an event):
```json
{
  "type": "model_cost_warn",
  "data": {
    "model": "anthropic/claude-opus-4-8",
    "cost_per_1m_input_usd": 15.0,
    "threshold_per_1m_input_usd": 5.0,
    "action": "model_override"
  }
}
```

### Seam 4 — TUI surface (`lifecycle_forwarder.py` + `right_panel/events_tab`)

**A. Inline conv-pane marker** (mirrors existing `on_budget_warn` pattern):
```python
def on_model_cost_warn(self, data: dict) -> None:
    cost = data.get("cost_per_1m_input_usd", "?")
    model = data.get("model", "?")
    self._enqueue(f"[⚠ high-cost model: {model} — ${cost:.2f}/1M input tokens]")
```
This surfaces immediately in the conversation pane without blocking the user.

**B. Events tab** — `model_cost_warn` events auto-appear in the existing Events tab
(no additional wiring needed — the events log already captures all event types).

**C. (Optional, deferred) Confirmation gate**: A pre-blocking confirmation before
the switch takes effect, using the existing `pending_intervention` /
`ask_user` path. Deferred to S4 — the S1–S3 warn-only path ships first so
the UX can be validated before adding a block.

---

## Staged implementation

| Stage | Scope | Owner |
|---|---|---|
| **S1** | `llm/model_cost_rate.py` utility + `CostWarnConfig` dataclass + config parser | tui-coder |
| **S2** | `/model` slash: emit `model_cost_warn` event on high-cost switch | tui-coder |
| **S3** | Session startup: emit on high-cost model at init | runtime-coder (crosses session.py) |
| **S4** | Optional blocking gate via `pending_intervention` | deferred, post-validation |

---

## Open questions for lead

**Q1 (threshold default)**: $5/1M proposed. Does this feel right for the target users?
claude-opus-4-8 is ~$15/1M, claude-sonnet-4-6 is ~$3/1M. A $5 threshold catches
Opus-class but not Sonnet. Or: should threshold be model-class-based (strong → always
warn) rather than absolute USD?

**Q2 (warn vs. block)** [RESOLVED]: S1–S3 warn-only = **shippable core** (satisfies "使う
前に気づかせる UX" = awareness). S4 blocking gate is **deferred**. At #1830 close,
owner intent is verified: "does awareness-warn satisfy '事前確認', or is a block gate
required?" — warn-only ships first, S4 is a potential-completion tracked until close.
Do not autonomous-decide "warn = done" without owner judgment.

**Q3 (S3 owner)**: Session startup injection crosses `session.py` (runtime territory).
Should S3 go to e2e-coder or stay with tui-coder?

**Q4 (suppress after first warn)** [RESOLVED]: Session-scoped de-dup: warn once per
model per session. Subsequent switches to the same model are silent. Implemented via
a `_cost_warned_models: set[str]` on the session.

---

## What this does NOT cover (out of scope per #1830)

- Provider actual-cost reconciliation (low-priority in issue)
- Cost spike detection (OpenClaw-style; separate follow-up)
- Per-call cost estimates before each LLM call (cumulative tracking already exists)
