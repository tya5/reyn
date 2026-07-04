"""Tier 1 Contract tests for the Pipeline expression evaluator (R1 grammar).

``reyn.core.pipeline.expr`` is the pipeline control plane's first code brick:
a pure, total, tree-walking evaluator for the expression language used by
``transform.value`` / ``until`` / ``verify.condition`` / ``fold.init``
(``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R1). These tests
pin the public surface (``parse`` / ``evaluate`` / ``evaluate_expr`` /
``ExprEvalError`` / ``ExprParseError``): the spec's own example expressions
must evaluate correctly, the combinator set must cover the reshape needs
appendix A/C call out, and the totality/safety properties (no recursion, no
calls, no IO) must hold structurally — not just as an untested claim.
"""
from __future__ import annotations

import pytest

from reyn.core.pipeline.expr import (
    Combinator,
    ExprEvalError,
    ExprParseError,
    Lambda,
    evaluate,
    evaluate_expr,
    parse,
)

# ---------------------------------------------------------------------------
# Spec's own example expressions (R1 notes + appendix B canonical example)
# ---------------------------------------------------------------------------


def test_all_combinator_matches_spec_example() -> None:
    """Tier 1: `all(results, r -> r.verified)` (R1 notes example) evaluates correctly."""
    ctx = {"results": [{"verified": True}, {"verified": True}]}
    assert evaluate_expr("all(results, r -> r.verified)", ctx) is True

    ctx_fail = {"results": [{"verified": True}, {"verified": False}]}
    assert evaluate_expr("all(results, r -> r.verified)", ctx_fail) is False


def test_object_construction_with_nested_combinator_matches_spec_example() -> None:
    """Tier 1: `{passed: all(...), items: results}` (R1 notes example) evaluates correctly."""
    ctx = {"results": [{"verified": True}, {"verified": True}]}
    out = evaluate_expr("{passed: all(results, r -> r.verified), items: results}", ctx)
    assert out == {"passed": True, "items": ctx["results"]}


def test_join_matches_spec_example() -> None:
    """Tier 1: `join(review.comments, "\\n")` (R1 notes example) evaluates correctly."""
    ctx = {"review": {"comments": ["fix typo", "add test"]}}
    out = evaluate_expr('join(review.comments, "\\n")', ctx)
    assert out == "fix typo\nadd test"


def test_empty_object_and_list_literals_match_spec_example() -> None:
    """Tier 1: `{glossary: {}, summaries: []}` (R1 notes example) evaluates correctly."""
    out = evaluate_expr("{glossary: {}, summaries: []}", {})
    assert out == {"glossary": {}, "summaries": []}


def test_dotted_ctx_path_access() -> None:
    """Tier 1: `ctx.review.passed` dotted static path access resolves nested context."""
    ctx = {"ctx": {"review": {"passed": True}}}
    assert evaluate_expr("ctx.review.passed", ctx) is True


def test_comparisons_and_booleans() -> None:
    """Tier 1: comparison and boolean operators (==, !=, <, >, and, or, not) evaluate correctly."""
    ctx = {"a": 3, "b": 5}
    assert evaluate_expr("a < b and not (a == b)", ctx) is True
    assert evaluate_expr("a > b or a == 3", ctx) is True
    assert evaluate_expr("a != b", ctx) is True


def test_refine_until_predicate_from_canonical_example() -> None:
    """Tier 1: `ctx.review.passed` as used in appendix B's `refine.until` evaluates correctly."""
    ctx = {"ctx": {"review": {"passed": False}}}
    assert evaluate_expr("ctx.review.passed", ctx) is False


# ---------------------------------------------------------------------------
# Reshape coverage (the calibration goal)
# ---------------------------------------------------------------------------


def test_map_pluck() -> None:
    """Tier 1: `map(list, x -> x.field)` projects (plucks) a field across a list."""
    ctx = {"items": [{"field": "a"}, {"field": "b"}]}
    assert evaluate_expr("map(items, x -> x.field)", ctx) == ["a", "b"]


def test_filter_keeps_matching_elements() -> None:
    """Tier 1: `filter(list, x -> pred)` keeps only elements where the lambda is true."""
    ctx = {"items": [{"n": 1}, {"n": 2}, {"n": 3}]}
    assert evaluate_expr("filter(items, x -> x.n > 1)", ctx) == [{"n": 2}, {"n": 3}]


def test_count_returns_list_length() -> None:
    """Tier 1: `count(list)` returns the list length."""
    assert evaluate_expr("count(items)", {"items": [1, 2, 3, 4]}) == 4


def test_sum_returns_numeric_total() -> None:
    """Tier 1: `sum(list)` returns the numeric sum of a list."""
    assert evaluate_expr("sum(items)", {"items": [1, 2, 3.5]}) == 6.5


def test_find_returns_first_match_or_null() -> None:
    """Tier 1: `find(list, x -> pred)` returns the first match, or null when none matches."""
    ctx = {"items": [{"n": 1}, {"n": 2}]}
    assert evaluate_expr("find(items, x -> x.n == 2)", ctx) == {"n": 2}
    assert evaluate_expr("find(items, x -> x.n == 99)", ctx) is None


def test_nested_object_construction() -> None:
    """Tier 1: object literals can nest and reference other paths in their field expressions."""
    ctx = {"a": 1, "b": {"c": 2}}
    out = evaluate_expr("{outer: {inner: b.c, echo: a}}", ctx)
    assert out == {"outer": {"inner": 2, "echo": 1}}


def test_any_combinator() -> None:
    """Tier 1: `any(list, x -> pred)` is existential quantification over the list."""
    ctx = {"items": [{"ok": False}, {"ok": True}]}
    assert evaluate_expr("any(items, x -> x.ok)", ctx) is True
    assert evaluate_expr("any(items, x -> x.ok)", {"items": [{"ok": False}]}) is False


def test_get_with_default_on_absent_path() -> None:
    """Tier 1: `get(expr, "path", default)` is safe access — absent path returns the default."""
    ctx = {"review": {"passed": True}}
    assert evaluate_expr('get(review, "passed")', ctx) is True
    assert evaluate_expr('get(review, "missing.nested", "fallback")', ctx) == "fallback"
    assert evaluate_expr('get(review, "missing.nested")', ctx) is None


# ---------------------------------------------------------------------------
# Totality / safety
# ---------------------------------------------------------------------------


def test_arbitrary_function_call_is_a_parse_error() -> None:
    """Tier 1: an IDENT(...) call outside the closed combinator set is a parse error, not IO."""
    with pytest.raises(ExprParseError):
        parse("shell('rm -rf /')")


def test_lambda_outside_a_combinator_is_a_parse_error() -> None:
    """Tier 1: a bare `x -> expr` (not inside a combinator argument slot) is a parse error."""
    with pytest.raises(ExprParseError):
        parse("x -> x")


def test_lambda_cannot_be_passed_as_a_value() -> None:
    """Tier 1: there is no syntax to bind/name/pass a lambda — only `IDENT -> Expr` inline."""
    with pytest.raises(ExprParseError):
        parse("map(items, f)")  # f is not a Lambda: "IDENT -> Expr" is required


def test_malformed_input_is_a_parse_error() -> None:
    """Tier 1: unterminated/incomplete/empty source strings raise ExprParseError."""
    with pytest.raises(ExprParseError):
        parse("{unterminated:")
    with pytest.raises(ExprParseError):
        parse("1 +")
    with pytest.raises(ExprParseError):
        parse("")


def test_bare_absent_path_is_an_eval_error() -> None:
    """Tier 1: a bare Path to an absent field raises ExprEvalError (R1 error semantics)."""
    with pytest.raises(ExprEvalError):
        evaluate_expr("ctx.missing.field", {"ctx": {}})


def test_type_errors_raise_eval_error() -> None:
    """Tier 1: type errors (count on non-list, + on incompatible types) raise ExprEvalError."""
    with pytest.raises(ExprEvalError):
        evaluate_expr("count(not_a_list)", {"not_a_list": 5})
    with pytest.raises(ExprEvalError):
        evaluate_expr("1 + 'x'", {})


def test_ast_has_no_call_or_recursion_construct() -> None:
    """Tier 1: the only invocation-shaped AST node is Combinator (closed name set);

    its Lambda argument (when present) is a leaf evaluated once per element,
    never itself invoked as a value — there is no user-defined-function or
    self-reference node in the AST.
    """
    node = parse("map(items, x -> x.n)")
    assert isinstance(node, Combinator)
    assert node.name == "map"
    assert isinstance(node.args[1], Lambda)


def test_totality_deep_nesting_still_terminates() -> None:
    """Tier 1: deep nesting is bounded by source length, not an unbounded runtime loop."""
    src = "1" + " + 1" * 200
    assert evaluate_expr(src, {}) == 201


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_expr_and_context_yields_same_result() -> None:
    """Tier 1: evaluating the same (AST, context) pair twice yields an identical result."""
    ctx = {"results": [{"verified": True}, {"verified": False}]}
    node = parse("{passed: all(results, r -> r.verified), n: count(results)}")
    first = evaluate(node, ctx)
    second = evaluate(node, ctx)
    assert first == second == {"passed": False, "n": 2}
