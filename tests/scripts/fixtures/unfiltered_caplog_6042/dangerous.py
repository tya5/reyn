"""Fixture: the dangerous (unfiltered) caplog-consumption shapes this
gate's classifier must catch -- one function per shape, mirroring the real
incidents (#6031/#6035/#6038; the 2 sites fixed in the PR that added this
gate; and the bare-truthiness/indexing/bare-iteration shapes the inverted
(allowlist-safe) classifier additionally catches, per lead-coder's
https://github.com/tya5/reyn/pull/6040#issuecomment-5611043646 -- a level-
or count-only filter with NO subject-specific check is the same hazard
class no matter which of these 6 syntax contexts it is finally consumed
through).

Not a real module — never imported, never collected as a test (functions
are deliberately NOT named `test_*`, matching `truly_silent.py`'s own
established convention in the sibling `silent_except_5990/` fixture dir —
a `test_`-prefixed name here would sweep these synthetic shapes into
`test_tier_audit.py`'s population, which happened once and was fixed by
this same rename: #6040 review thread). `caplog` is a plain parameter
name here, not the pytest fixture — these functions are never called,
only AST-parsed by `find_violations()`."""
import logging


def shape_equality_with_literal_empty(caplog):
    assert caplog.records == []


def shape_len_comparison(caplog):
    assert len(caplog.records) == 2


def shape_tuple_unpacking(caplog):
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    (only,) = warnings
    assert only


def shape_unfiltered_any(caplog):
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def shape_bare_truthiness_assert_not(caplog):
    """The exact miss the enumerated-dangerous-shapes classifier had: a
    level-only (non-subject-specific) filter, consumed by truthiness --
    same hazard class as `caplog.records == []`, different spelling."""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings


def shape_bare_truthiness_if(caplog):
    if caplog.records:
        raise AssertionError("unexpected log line")


def shape_indexing_into_unfiltered_derived_list(caplog):
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "boom" in warnings[0].getMessage()


def shape_bare_iteration_without_subject_filter(caplog):
    for r in caplog.records:
        assert r.levelno < logging.WARNING
