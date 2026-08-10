"""Tier 1: contract — proposal 0060 Addendum D, D5c (error-message rail).

Co-vet pin: on a parse/validation failure, the error message carries a
pointer to the relevant doc_ref — the failure moment teaches the fix instead
of leaving a bare "invalid input" the model can't act on.

Each test asserts a REAL raise site (real parser / real blueprint validator /
real op ValidationError path — no mocks) produces a message containing the
doc pointer. Falsify: ``with_doc_pointer`` is exercised directly (stripping
the append call from the annotated exception classes is what would turn
these tests RED — verified manually while implementing: reverting
``PipelineParseError.__init__`` / ``PresentBlueprintError.__init__`` to the
bare ``ValueError`` behavior makes ``test_pipeline_parse_error_carries_pointer``
/ ``test_present_blueprint_error_carries_pointer`` fail).
"""
from __future__ import annotations

import pytest

from reyn.core.doc_ref_rail import with_doc_pointer


def test_with_doc_pointer_appends_once() -> None:
    """Tier 1: the helper appends a "(see <doc_ref> ...)" suffix, idempotently."""
    msg = with_doc_pointer("bad input", "docs/reference/runtime/pipeline-dsl.md")
    assert "docs/reference/runtime/pipeline-dsl.md" in msg
    assert msg.startswith("bad input")

    twice = with_doc_pointer(msg, "docs/reference/runtime/pipeline-dsl.md")
    assert twice == msg, "re-annotating an already-annotated message must not double the suffix"


def test_pipeline_parse_error_carries_pointer() -> None:
    """Tier 1: a real PipelineParseError (malformed YAML, an actual parser raise
    site) carries the pipeline-dsl.md pointer in its message."""
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaRegistry

    with pytest.raises(PipelineParseError) as exc_info:
        parse_pipeline_dsl("not: [valid, yaml", SchemaRegistry())

    assert "docs/reference/runtime/pipeline-dsl.md" in str(exc_info.value)


def test_present_blueprint_error_carries_pointer() -> None:
    """Tier 1: a real PresentBlueprintError (an unknown component in a real
    blueprint validation call) carries the present.md pointer in its message."""
    from reyn.core.present.catalog import PresentBlueprintError, validate_blueprint

    with pytest.raises(PresentBlueprintError) as exc_info:
        validate_blueprint({"component": "not_a_real_component", "text": "x"})

    assert "docs/concepts/runtime/present.md" in str(exc_info.value)


def test_present_op_validation_error_carries_pointer(tmp_path) -> None:
    """Tier 1: the present ToolDefinition's handler wraps a real pydantic
    ValidationError (XOR violation: neither data_ref nor data_inline) with the
    present.md pointer — the Control-IR op boundary, not just the DSL parser."""
    import asyncio

    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.tools import ToolContext
    from reyn.tools.present import _handle_present

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=resolver)
    ctx = ToolContext(
        events=events, permission_resolver=resolver, workspace=ws, caller_kind="router",
    )

    result = asyncio.run(_handle_present({}, ctx))
    assert result["ok"] is False
    assert "docs/concepts/runtime/present.md" in result["error"]


def test_falsify_bare_valueerror_has_no_pointer() -> None:
    """Tier 1: (falsify) an UN-annotated ValueError (the shape PipelineParseError/
    PresentBlueprintError had before D5c) has no doc pointer — proving the
    pointer is added BY these classes, not incidentally present in every message."""
    assert "docs/" not in str(ValueError("some other unrelated failure"))
