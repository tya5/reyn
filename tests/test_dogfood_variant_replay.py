"""Tier 2: dogfood_variant_replay — config parsing + classifier + table
rendering.

The full ``run_ablation`` end-to-end path is NOT covered by these tests
(= it shells out to ``llm_replay.py`` which hits the LiteLLM proxy +
costs LLM tokens; that's the script's purpose). Instead these tests
pin the pure-function pieces:

- YAML config parsing (= structural validation, required-key
  enforcement, ordering preservation).
- Classifier first-match semantics + fallback bucket.
- Last-JSON extraction from llm_replay stdout output (= the parse
  contract this script depends on).
- Markdown table rendering (= column ordering matches classifier
  list, UNCLASSIFIED bucket only appears when needed).

testing.ja.md compliance:
- No ``unittest.mock.patch``. ``pytest.monkeypatch`` is used only
  where applicable (= module-attribute setup, not faking
  collaborators).
- ``classify_response`` is exercised via real input dicts.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

# scripts/ is not on sys.path during normal test discovery; add it
# lazily so the script module is importable without restructuring.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dogfood_variant_replay import (  # noqa: E402
    ClassifierRule,
    RunConfig,
    Variant,
    _extract_last_json,
    classify_response,
    load_config,
    render_table,
)

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ablation.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_config_round_trips_required_fields(tmp_path):
    """Tier 2: a well-formed config produces a RunConfig with the
    declared trace / req_id / model / n / variants preserved.
    """
    cfg_path = _write_config(tmp_path, """
trace: /tmp/foo.jsonl
req_id: abc-123
model: openai/test-model
n: 5
classifiers:
  - {label: EMPTY, expr: 'not content'}
  - {label: OTHER, expr: 'True'}
variants:
  - {name: A, patches: []}
  - {name: B, patches: ['x.y=z']}
""")
    cfg = load_config(cfg_path)
    assert cfg.trace == "/tmp/foo.jsonl"
    assert cfg.req_id == "abc-123"
    assert cfg.model == "openai/test-model"
    assert cfg.n == 5
    assert cfg.classifiers  # classifiers loaded
    assert cfg.classifiers[0].label == "EMPTY"
    assert any(c.label == "OTHER" for c in cfg.classifiers), "second classifier label must be present"
    assert cfg.variants  # variants loaded
    assert cfg.variants[1].patches == ("x.y=z",)


def test_load_config_rejects_missing_required_key(tmp_path):
    """Tier 2: omitting a required key surfaces a clean ValueError,
    not a downstream KeyError mid-run."""
    cfg_path = _write_config(tmp_path, """
req_id: abc
model: m
n: 1
classifiers:
  - {label: X, expr: 'True'}
variants:
  - {name: A, patches: []}
""")  # missing 'trace'
    with pytest.raises(ValueError, match="trace"):
        load_config(cfg_path)


def test_load_config_rejects_empty_variants(tmp_path):
    """Tier 2: empty variants list is invalid (= nothing to compare)."""
    cfg_path = _write_config(tmp_path, """
trace: /tmp/foo.jsonl
req_id: a
model: m
n: 1
classifiers:
  - {label: X, expr: 'True'}
variants: []
""")
    with pytest.raises(ValueError, match="variants"):
        load_config(cfg_path)


def test_load_config_rejects_empty_classifiers(tmp_path):
    """Tier 2: empty classifiers list is invalid (= no way to bucket
    results)."""
    cfg_path = _write_config(tmp_path, """
trace: /tmp/foo.jsonl
req_id: a
model: m
n: 1
classifiers: []
variants:
  - {name: A, patches: []}
""")
    with pytest.raises(ValueError, match="classifiers"):
        load_config(cfg_path)


def test_load_config_parallel_defaults_to_8(tmp_path):
    """Tier 2: parallel field is optional with a sane default."""
    cfg_path = _write_config(tmp_path, """
trace: /tmp/foo.jsonl
req_id: a
model: m
n: 1
classifiers: [{label: X, expr: 'True'}]
variants: [{name: A, patches: []}]
""")
    cfg = load_config(cfg_path)
    assert cfg.parallel == 8


# ---------------------------------------------------------------------------
# Classifier: first-match semantics + fallback
# ---------------------------------------------------------------------------


def _classifiers(*pairs: tuple[str, str]) -> tuple[ClassifierRule, ...]:
    return tuple(ClassifierRule(label=l, expr=e) for l, e in pairs)


def test_classify_response_empty_response_matches_empty_rule():
    """Tier 2: ``not content and not tool_calls`` matches the
    empty-stop response shape (= finish=stop, content=None / "",
    tool_calls=[])."""
    rules = _classifiers(
        ("EMPTY", "not content and not tool_calls"),
        ("OTHER", "True"),
    )
    label = classify_response({"content": "", "tool_calls": []}, rules)
    assert label == "EMPTY"


def test_classify_response_substantive_content_skips_empty_rule():
    """Tier 2: a response with substantive content does NOT match the
    empty rule even though tool_calls is empty (= classifier ordering
    is first-match)."""
    rules = _classifiers(
        ("EMPTY", "not content"),
        ("CONTENT", "len(content) > 0"),
        ("OTHER", "True"),
    )
    label = classify_response(
        {"content": "Skill running...", "tool_calls": []}, rules,
    )
    assert label == "CONTENT"


def test_classify_response_first_match_wins():
    """Tier 2: when multiple classifiers would match, the first one in
    config order wins. This is the user-facing semantic that lets
    callers use earlier specific rules + later catch-all."""
    rules = _classifiers(
        ("NONEMPTY", "content != ''"),
        ("ALWAYS", "True"),
        ("OTHER", "True"),
    )
    # "tiny" matches both NONEMPTY and ALWAYS — first-match must return NONEMPTY
    label = classify_response({"content": "tiny", "tool_calls": []}, rules)
    assert label == "NONEMPTY"


def test_classify_response_fallback_to_unclassified_when_no_match():
    """Tier 2: if no rule matches (= user forgot a catch-all), the
    label is ``UNCLASSIFIED`` rather than crashing or omitting the
    sample from counts."""
    rules = _classifiers(
        ("NEVER", "False"),
        ("ALSO_NEVER", "False"),
    )
    label = classify_response({"content": "x", "tool_calls": []}, rules)
    assert label == "UNCLASSIFIED"


def test_classify_response_eval_error_falls_through_to_next_rule():
    """Tier 2: a broken classifier expression doesn't crash the
    classifier — it falls through to the next rule. Catches typos
    in caller YAML without halting the batch."""
    rules = _classifiers(
        ("BROKEN", "undefined_name + 1"),   # NameError
        ("OK", "True"),
    )
    label = classify_response({"content": "x", "tool_calls": []}, rules)
    assert label == "OK"


def test_classify_response_exposes_content_tool_calls_finish_reason():
    """Tier 2: all three documented locals (content / tool_calls /
    finish_reason) are addressable in classifier expressions.
    """
    rules = _classifiers(
        ("STOP_EMPTY", "finish_reason == 'stop' and not content"),
        ("TOOL", "bool(tool_calls)"),
        ("OTHER", "True"),
    )
    label1 = classify_response(
        {"content": "", "tool_calls": [], "finish_reason": "stop"}, rules,
    )
    assert label1 == "STOP_EMPTY"
    label2 = classify_response(
        {"content": "", "tool_calls": [{"function": {"name": "x"}}]}, rules,
    )
    assert label2 == "TOOL"


# ---------------------------------------------------------------------------
# JSON extraction from llm_replay output
# ---------------------------------------------------------------------------


def test_extract_last_json_finds_object_after_preamble():
    """Tier 2: llm_replay prints a preamble before the JSON when
    ``--output-format json`` (= banner + patch summary). Extractor
    finds the JSON object regardless of preamble lines."""
    text = """=== LLM Replay ===
  request_id: abc-123
  model: foo
  patches applied: 1
{"content": "hello", "tool_calls": []}"""
    out = _extract_last_json(text)
    assert out == {"content": "hello", "tool_calls": []}


def test_extract_last_json_returns_empty_on_no_json():
    """Tier 2: error path (= subprocess failed, no JSON in stdout)
    returns ``{}`` so the caller's classifier falls into the
    UNCLASSIFIED bucket rather than crashing."""
    assert _extract_last_json("error: something bad\n") == {}


def test_extract_last_json_handles_multiline_json():
    """Tier 2: when the JSON spans multiple lines (= pretty-printed
    output), the extractor consumes all lines from the first '{' to
    end-of-stream."""
    text = """preamble line
{
  "content": "multiline",
  "tool_calls": []
}"""
    out = _extract_last_json(text)
    assert out["content"] == "multiline"


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


def test_render_table_columns_match_classifier_order():
    """Tier 2: table columns follow the user-supplied classifier
    order, not alphabetical or insertion order on the counter.
    Reverse alphabetical input + verify column order preserved.
    """
    classifiers = _classifiers(
        ("Z_first", "True"),  # alphabetically last but listed first
        ("A_last", "False"),
    )
    results = {
        "v1": Counter({"Z_first": 3, "A_last": 1}),
    }
    table = render_table(results, classifiers, n=4)
    # "Z_first" header must appear before "A_last" in the rendered table
    assert table.index("Z_first") < table.index("A_last")


def test_render_table_omits_unclassified_when_zero():
    """Tier 2: UNCLASSIFIED column only appears in the table when at
    least one variant has unclassified samples — keeps the table
    clean for well-formed configs."""
    classifiers = _classifiers(("X", "True"))
    results = {"v1": Counter({"X": 5})}  # no UNCLASSIFIED
    table = render_table(results, classifiers, n=5)
    assert "UNCLASSIFIED" not in table


def test_render_table_includes_unclassified_when_nonzero():
    """Tier 2: UNCLASSIFIED column is added when any variant has
    UNCLASSIFIED counts (= signals the user's classifier is incomplete)."""
    classifiers = _classifiers(("X", "False"))  # catches nothing
    results = {"v1": Counter({"UNCLASSIFIED": 5})}
    table = render_table(results, classifiers, n=5)
    assert "UNCLASSIFIED" in table


def test_render_table_shows_count_over_n_form():
    """Tier 2: each cell displays ``count/n`` so the reader sees both
    the absolute count and the sample size at a glance."""
    classifiers = _classifiers(("ACK", "True"))
    results = {"variant_d": Counter({"ACK": 7})}
    table = render_table(results, classifiers, n=10)
    assert "7/10" in table
