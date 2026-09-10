"""Fixture: subject-specific (safe) caplog-consumption shapes this gate's
classifier must NOT flag -- one function per real idiom found across the
tree's 108 already-safe sites (logger-name filter, message-content
filter, containment check, two-stage derived-variable filter).

Not a real module — never imported, never collected as a test (functions
are deliberately NOT named `test_*`, matching `truly_silent.py`'s own
established convention in the sibling `silent_except_5990/` fixture dir —
see `dangerous.py` in this same directory for why)."""
import logging


def shape_message_content_membership(caplog):
    assert any("NOPE.md" in r.message for r in caplog.records)


def shape_logger_name_filtered_equality_empty(caplog):
    assert [r for r in caplog.records if r.name == "reyn.x"] == []


def shape_logger_name_filtered_any_predicate(caplog):
    model = "gpt-4o"
    assert any(
        r.name == "reyn.runtime.router_loop" and model in r.getMessage()
        for r in caplog.records
    )


def shape_text_containment(caplog):
    assert "unresponsive" in caplog.text
    assert "recovered" not in caplog.text


def shape_two_stage_derived_membership(caplog):
    messages = [r.message for r in caplog.records]
    assert any("y" in m for m in messages)
    assert not any("z" in m for m in messages)


def shape_level_only_filter_then_subject_filtered_membership(caplog):
    """A level-only filter (no subject check in the filter itself) that
    is THEN consumed through a subject-specific containment check must
    not be flagged -- the containment check is what makes the final
    consumption safe, independent of whether the intermediate `warnings`
    derivation was itself subject-filtered."""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("x" in r.getMessage() for r in warnings)
