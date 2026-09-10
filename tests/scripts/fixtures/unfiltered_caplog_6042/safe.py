"""Fixture: subject-specific (safe) caplog-consumption shapes this gate's
classifier must NOT flag -- one function per real idiom found across the
tree's 108 already-safe sites (logger-name filter, message-content
filter, containment check, two-stage derived-variable filter)."""
import logging


def test_message_content_membership(caplog):
    assert any("NOPE.md" in r.message for r in caplog.records)


def test_logger_name_filtered_equality_empty(caplog):
    assert [r for r in caplog.records if r.name == "reyn.x"] == []


def test_logger_name_filtered_any_predicate(caplog):
    model = "gpt-4o"
    assert any(
        r.name == "reyn.runtime.router_loop" and model in r.getMessage()
        for r in caplog.records
    )


def test_text_containment(caplog):
    assert "unresponsive" in caplog.text
    assert "recovered" not in caplog.text


def test_two_stage_derived_membership(caplog):
    messages = [r.message for r in caplog.records]
    assert any("y" in m for m in messages)
    assert not any("z" in m for m in messages)


def test_level_only_filter_never_reaches_a_dangerous_shape(caplog):
    """A level-only filter (no subject check) that is only ever
    truthy-tested or indexed -- never compared/len()'d/unpacked -- must
    not be flagged either (it never reaches a dangerous consumption
    shape at all)."""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert "x" in warnings[0].getMessage()
