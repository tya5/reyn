"""Dogfood scenario set schema + YAML loader (FP-0036 Component A).

Defines the in-memory data model for ``dogfood/scenarios/*.yaml`` and the
``load_scenario_set`` function consumed by the runner (F2), verifiers (F3),
coverage matrix (F4), and replay (F5).

Schema (= top-level frontmatter / list under ``scenarios:``):

    type: dogfood_scenario_set   # required, exact string
    name: <set_id>               # required, unique within the repo
    description: <str>           # optional
    covers: [<feature_path>]     # set-level coverage tags
    scenarios:
      - id: <scenario_id>        # required, unique within the set
        covers: [<feature_path>] # per-scenario coverage tags
        input: <str>             # single-turn prompt
        prompts: [<str>]         # OR multi-turn prompt list (mutually exclusive with input)
        kind: <str>              # optional categorisation tag (matches long_session_v1.yaml)
        expected:
          reply: {kind, rubric/value}  # see ExpectedReply below
          events: {must_emit, must_not_emit, sequence}  # ExpectedEvents
          artifacts: [{...}]             # ExpectedArtifacts
        outcome_prediction:        # 4-band distribution, summing to ~1.0
          verified: 0.7
          inconclusive: 0.2
          refuted: 0.05
          blocked: 0.05

Backward compatibility note:
  The existing ``long_session_v1.yaml`` uses a ``metadata:`` top-level key
  instead of a flat ``name:`` / ``description:`` / ``type:``. This loader
  accepts that legacy shape transparently: if ``type`` is absent but
  ``metadata.name`` is present, the document is treated as a
  ``dogfood_scenario_set`` without ``expected_*`` fields. Scenarios with only
  ``prompts:`` and ``kind:`` (no ``expected:``) are also accepted — their
  ``expected_*`` fields remain ``None``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


class ScenarioLoadError(ValueError):
    """Raised when a scenario YAML fails schema validation."""


# ── reply ─────────────────────────────────────────────────────────────────

@dataclass
class ExpectedReply:
    """Reply text assertion.

    kind:
      - "judge": ``rubric`` is a list of natural-language criteria. The
        verifier (F3) invokes ``judge_output`` op against the rubric.
      - "substring": ``value`` is a string that must appear in the reply.
      - "exact": ``value`` is an exact match string (trimmed).
      - "regex": ``value`` is a regex pattern (re.search semantics).
    """
    kind: Literal["judge", "substring", "exact", "regex"]
    rubric: list[str] = field(default_factory=list)
    value: str = ""


# ── events ────────────────────────────────────────────────────────────────

@dataclass
class EventAssertion:
    """One event-emission assertion.

    type: event type name (= matches ``Event.type``)
    count: comparator string, e.g. ">=1", "==2", "<5"; default ">=1"
    payload: dict of key→expected value (subset match on event.data)
    status: optional shortcut — restricts to events whose payload.status equals this
    """
    type: str
    count: str = ">=1"
    payload: dict = field(default_factory=dict)
    status: str | None = None


@dataclass
class ExpectedEvents:
    must_emit: list[EventAssertion] = field(default_factory=list)
    must_not_emit: list[EventAssertion] = field(default_factory=list)
    sequence: list[str] = field(default_factory=list)  # ordered subsequence of event types


# ── artifacts ─────────────────────────────────────────────────────────────

@dataclass
class ArtifactAssertion:
    """One workspace-artifact assertion.

    skill: source skill name (= the spawning skill)
    type: artifact type name (= matches ``Artifact.type``)
    present: True/False — assert artifact exists / doesn't exist
    fingerprint: optional SHA256 hex of normalised content; pinned regression
    """
    skill: str | None = None
    type: str | None = None
    present: bool = True
    fingerprint: str | None = None


@dataclass
class ExpectedArtifacts:
    items: list[ArtifactAssertion] = field(default_factory=list)


# ── outcome prediction ────────────────────────────────────────────────────

@dataclass
class OutcomePrediction:
    """4-band outcome distribution (= dogfood-discipline.ja.md 4-outcome).

    Must sum to ~1.0 (allow 0.001 tolerance). Used for Brier scoring.
    """
    verified: float = 0.0
    inconclusive: float = 0.0
    refuted: float = 0.0
    blocked: float = 0.0


# ── scenario ──────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    id: str
    covers: list[str] = field(default_factory=list)
    input: str | None = None              # single-turn
    prompts: list[str] = field(default_factory=list)  # multi-turn; mutually exclusive with input
    kind: str | None = None
    expected_reply: ExpectedReply | None = None
    expected_events: ExpectedEvents | None = None
    expected_artifacts: ExpectedArtifacts | None = None
    outcome_prediction: OutcomePrediction | None = None

    @property
    def is_multi_turn(self) -> bool:
        return bool(self.prompts)


# ── scenario set ──────────────────────────────────────────────────────────

@dataclass
class ScenarioSet:
    name: str
    description: str = ""
    covers: list[str] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    source_path: str = ""  # absolute path the set was loaded from

    def scenario_by_id(self, scenario_id: str) -> Scenario | None:
        for s in self.scenarios:
            if s.id == scenario_id:
                return s
        return None


# ── internal helpers ──────────────────────────────────────────────────────

_COUNT_PATTERN = re.compile(r'^(==|>=|<=|<|>)?(\d+)$')

_VALID_REPLY_KINDS = {"judge", "substring", "exact", "regex"}


def _validate_count(count: str, context: str) -> None:
    """Raise ScenarioLoadError if count comparator syntax is invalid.

    Accepted forms: ``==N``, ``>=N``, ``<=N``, ``<N``, ``>N``, ``N``
    (bare integer treated as ``==N``).
    """
    if not _COUNT_PATTERN.match(str(count)):
        raise ScenarioLoadError(
            f"{context}: invalid count comparator {count!r}. "
            "Expected forms: '>=1', '==2', '<=5', '<3', '>0', or plain integer."
        )


def _parse_event_assertion(raw: Any, context: str) -> EventAssertion:
    if not isinstance(raw, dict):
        raise ScenarioLoadError(f"{context}: event assertion must be a mapping, got {type(raw).__name__}")
    event_type = raw.get("type")
    if not event_type:
        raise ScenarioLoadError(f"{context}: event assertion missing 'type' field")
    count = str(raw.get("count", ">=1"))
    _validate_count(count, f"{context}/type={event_type}")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ScenarioLoadError(f"{context}/type={event_type}: 'payload' must be a mapping")
    status = raw.get("status")
    return EventAssertion(type=str(event_type), count=count, payload=payload, status=status)


def _parse_expected_events(raw: Any, context: str) -> ExpectedEvents:
    if not isinstance(raw, dict):
        raise ScenarioLoadError(f"{context}: 'events' must be a mapping")
    must_emit = [
        _parse_event_assertion(item, f"{context}/must_emit[{i}]")
        for i, item in enumerate(raw.get("must_emit", []))
    ]
    must_not_emit = [
        _parse_event_assertion(item, f"{context}/must_not_emit[{i}]")
        for i, item in enumerate(raw.get("must_not_emit", []))
    ]
    sequence = raw.get("sequence", [])
    if not isinstance(sequence, list):
        raise ScenarioLoadError(f"{context}: 'sequence' must be a list of event type strings")
    return ExpectedEvents(must_emit=must_emit, must_not_emit=must_not_emit, sequence=sequence)


def _parse_expected_reply(raw: Any, context: str) -> ExpectedReply:
    if not isinstance(raw, dict):
        raise ScenarioLoadError(f"{context}: 'reply' must be a mapping")
    kind = raw.get("kind")
    if kind not in _VALID_REPLY_KINDS:
        raise ScenarioLoadError(
            f"{context}: 'reply.kind' must be one of {sorted(_VALID_REPLY_KINDS)}, got {kind!r}"
        )
    rubric = raw.get("rubric", [])
    value = raw.get("value", "")
    if kind == "judge":
        if not rubric:
            raise ScenarioLoadError(
                f"{context}: reply kind='judge' requires a non-empty 'rubric' list"
            )
        if not isinstance(rubric, list):
            raise ScenarioLoadError(f"{context}: 'rubric' must be a list of strings")
    else:
        if not value:
            raise ScenarioLoadError(
                f"{context}: reply kind={kind!r} requires a non-empty 'value' string"
            )
    return ExpectedReply(kind=kind, rubric=list(rubric) if rubric else [], value=str(value) if value else "")


def _parse_artifact_assertion(raw: Any, context: str) -> ArtifactAssertion:
    if not isinstance(raw, dict):
        raise ScenarioLoadError(f"{context}: artifact assertion must be a mapping")
    return ArtifactAssertion(
        skill=raw.get("skill"),
        type=raw.get("type"),
        present=bool(raw.get("present", True)),
        fingerprint=raw.get("fingerprint"),
    )


def _parse_expected_artifacts(raw: Any, context: str) -> ExpectedArtifacts:
    if isinstance(raw, list):
        items = [_parse_artifact_assertion(item, f"{context}[{i}]") for i, item in enumerate(raw)]
    elif isinstance(raw, dict):
        # Allow mapping form with 'items' key
        items = [
            _parse_artifact_assertion(item, f"{context}/items[{i}]")
            for i, item in enumerate(raw.get("items", []))
        ]
    else:
        raise ScenarioLoadError(f"{context}: 'artifacts' must be a list or mapping")
    return ExpectedArtifacts(items=items)


def _parse_outcome_prediction(raw: Any, context: str) -> OutcomePrediction:
    if not isinstance(raw, dict):
        raise ScenarioLoadError(f"{context}: 'outcome_prediction' must be a mapping")
    try:
        verified = float(raw.get("verified", 0.0))
        inconclusive = float(raw.get("inconclusive", 0.0))
        refuted = float(raw.get("refuted", 0.0))
        blocked = float(raw.get("blocked", 0.0))
    except (TypeError, ValueError) as exc:
        raise ScenarioLoadError(f"{context}: outcome_prediction values must be numeric: {exc}") from exc

    total = verified + inconclusive + refuted + blocked
    if abs(total - 1.0) > 0.001:
        raise ScenarioLoadError(
            f"{context}: outcome_prediction must sum to 1.0 ± 0.001, got {total:.6f}"
        )
    return OutcomePrediction(
        verified=verified,
        inconclusive=inconclusive,
        refuted=refuted,
        blocked=blocked,
    )


def _parse_scenario(raw: Any, index: int, source: str) -> Scenario:
    if not isinstance(raw, dict):
        raise ScenarioLoadError(f"{source}: scenario[{index}] must be a mapping")

    scenario_id = raw.get("id")
    if not scenario_id:
        raise ScenarioLoadError(f"{source}: scenario[{index}] missing required 'id' field")
    scenario_id = str(scenario_id)
    ctx = f"{source}/scenario:{scenario_id}"

    covers = raw.get("covers", [])
    if not isinstance(covers, list):
        covers = [covers]
    covers = [str(c) for c in covers]

    input_val = raw.get("input")
    prompts = raw.get("prompts", [])
    kind = raw.get("kind")

    has_input = input_val is not None and str(input_val).strip() != ""
    has_prompts = bool(prompts)

    if has_input and has_prompts:
        raise ScenarioLoadError(
            f"{ctx}: 'input' and 'prompts' are mutually exclusive — set only one"
        )
    if not has_input and not has_prompts:
        raise ScenarioLoadError(
            f"{ctx}: at least one of 'input' or 'prompts' is required"
        )

    # Parse expected block (optional — absent in legacy YAML)
    expected_raw = raw.get("expected")
    expected_reply: ExpectedReply | None = None
    expected_events: ExpectedEvents | None = None
    expected_artifacts: ExpectedArtifacts | None = None

    if expected_raw is not None:
        if not isinstance(expected_raw, dict):
            raise ScenarioLoadError(f"{ctx}: 'expected' must be a mapping")
        if "reply" in expected_raw:
            expected_reply = _parse_expected_reply(expected_raw["reply"], f"{ctx}/expected/reply")
        if "events" in expected_raw:
            expected_events = _parse_expected_events(expected_raw["events"], f"{ctx}/expected/events")
        if "artifacts" in expected_raw:
            expected_artifacts = _parse_expected_artifacts(
                expected_raw["artifacts"], f"{ctx}/expected/artifacts"
            )

    outcome_prediction: OutcomePrediction | None = None
    if "outcome_prediction" in raw:
        outcome_prediction = _parse_outcome_prediction(raw["outcome_prediction"], ctx)

    return Scenario(
        id=scenario_id,
        covers=covers,
        input=str(input_val) if has_input else None,
        prompts=[str(p) for p in prompts] if has_prompts else [],
        kind=str(kind) if kind is not None else None,
        expected_reply=expected_reply,
        expected_events=expected_events,
        expected_artifacts=expected_artifacts,
        outcome_prediction=outcome_prediction,
    )


# ── loader ────────────────────────────────────────────────────────────────

def load_scenario_set(path: str | Path) -> ScenarioSet:
    """Parse a dogfood scenario set YAML.

    Raises ScenarioLoadError on:
      - file not found
      - YAML parse error
      - missing/invalid top-level type / name / scenarios
      - per-scenario validation failure (= clear error naming the scenario)
      - mutually exclusive input + prompts
      - count comparator syntax error
      - outcome_prediction sum off by > 0.001

    Backward compatibility:
      The legacy ``long_session_v1.yaml`` format omits ``type:`` and uses a
      ``metadata:`` sub-key for ``name``/``description``. This is accepted
      transparently — ``expected_*`` fields remain None for all scenarios.
      If both ``metadata.name`` and a top-level ``name`` are present, the
      top-level ``name`` takes precedence.
    """
    path = Path(path)
    if not path.exists():
        raise ScenarioLoadError(f"Scenario set file not found: {path}")

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioLoadError(f"YAML parse error in {path}: {exc}") from exc

    if not isinstance(doc, dict):
        raise ScenarioLoadError(f"{path}: top-level document must be a YAML mapping")

    source = str(path.resolve())

    # ── determine if legacy format ───────────────────────────────────────
    # Legacy = has no 'type' key but has a 'metadata' sub-key with 'name'.
    # We do NOT raise on missing 'type' for legacy docs; we just treat it
    # as dogfood_scenario_set without expected fields.
    doc_type = doc.get("type")
    metadata = doc.get("metadata", {})

    if doc_type is None and isinstance(metadata, dict) and metadata.get("name"):
        # Legacy long_session_v1.yaml shape — accepted for backward compat
        is_legacy = True
    elif doc_type == "dogfood_scenario_set":
        is_legacy = False
    else:
        raise ScenarioLoadError(
            f"{path}: 'type' must be 'dogfood_scenario_set', got {doc_type!r}"
        )

    # Prefer top-level 'name'; fall back to metadata.name for legacy docs
    name = doc.get("name") or (metadata.get("name") if is_legacy else None)
    if not name:
        raise ScenarioLoadError(f"{path}: missing required 'name' field")
    name = str(name)

    description = doc.get("description") or (
        metadata.get("description", "") if is_legacy else ""
    )

    covers = doc.get("covers", [])
    if not isinstance(covers, list):
        covers = [covers]
    covers = [str(c) for c in covers]

    raw_scenarios = doc.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise ScenarioLoadError(
            f"{path}: 'scenarios' must be a non-empty list"
        )

    seen_ids: set[str] = set()
    scenarios: list[Scenario] = []
    for i, raw in enumerate(raw_scenarios):
        scenario = _parse_scenario(raw, i, source)
        if scenario.id in seen_ids:
            raise ScenarioLoadError(
                f"{source}: duplicate scenario id {scenario.id!r} at index {i}"
            )
        seen_ids.add(scenario.id)
        scenarios.append(scenario)

    return ScenarioSet(
        name=name,
        description=str(description),
        covers=covers,
        scenarios=scenarios,
        source_path=source,
    )
