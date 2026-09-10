"""Fixture: the dangerous (unfiltered) caplog-consumption shapes this
gate's classifier must catch -- one function per shape, mirroring the 4
real incidents (#6031/#6035/#6038 plus the 2 remaining sites fixed in the
same PR that adds this gate)."""
import logging


def test_equality_with_literal_empty(caplog):
    assert caplog.records == []


def test_len_comparison(caplog):
    assert len(caplog.records) == 2


def test_tuple_unpacking(caplog):
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    (only,) = warnings
    assert only


def test_unfiltered_any(caplog):
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
