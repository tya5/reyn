"""Tier 1: contract — a multi-component blueprint is expressible in the schema
the model is constrained by, not only in the op that receives it.

``validate_blueprint`` accepts "a single component node OR a list of nodes", and
a list is the only way to express a SEQUENCE of components. The tool schema
handed to the LLM declared ``blueprint: {"type": "object"}``, so the list form
was unrepresentable — a model wanting a heading followed by a table could not
say so, whatever the op would have accepted.

Observed live (#3630): asked to show data, a model emitted its component list
inside ``data_inline`` — the only other object-typed slot on the call. Read as
misuse it looks like a model error; read against the schema it is the only
place the value could go.

``array`` costs nothing in expressiveness. A single node is a one-element list,
while no multi-node sequence can be written as an object — so the object-only
declaration was strictly narrower than the op, in the one direction that
mattered. Declaring both is not available: the Gemini-safe rules enforced by
``test_router_tools.py`` forbid ``anyOf``.

**Why no existing test could have caught this.** All 26 test files touching the
present op build the op directly in Python and pass a list, which never travels
through the schema the model is bound by. The general form, worth stating
because it is not specific to present: *a test that constructs an op directly
cannot witness whether the model can REACH that op* — mechanism correctness and
reachability need separate witnesses. This file is the reachability half, and it
validates against the real published schema rather than asserting a type
literal, so it keeps holding if the schema is expressed some other way.
"""
from __future__ import annotations

import jsonschema
import pytest

from reyn.runtime.router_tools import build_tools
from reyn.schemas.models import PresentIROp

# The owner's live case: a text component followed by a markdown one. Two nodes
# is the point — one node is representable either way, so a single-node example
# would pass against the old declaration too and witness nothing.
_TWO_COMPONENT_BLUEPRINT = [
    {"component": "text", "text": "plain line"},
    {"component": "markdown", "text": "**bold** line"},
]


def _present_parameters() -> dict:
    """The parameter schema as actually published to the model."""
    for tool in build_tools():
        function = tool["function"]
        if function["name"] == "present":
            return function["parameters"]
    pytest.fail("present is not among the tools offered to the model")


def test_present_is_offered_to_the_model() -> None:
    """Tier 1: the tool reaches the model at all.

    Everything below validates against the published schema, so a present that
    stopped being published would make those assertions vacuous rather than red.
    """
    assert _present_parameters()["properties"].keys() >= {
        "data_ref", "data_inline", "view", "blueprint",
    }


def test_a_multi_component_blueprint_is_representable_to_the_model() -> None:
    """Tier 1: a two-node blueprint validates against the published schema."""
    schema = _present_parameters()

    jsonschema.validate(
        {"data_inline": {"k": "v"}, "blueprint": _TWO_COMPONENT_BLUEPRINT},
        schema,
    )


def test_the_op_accepts_what_the_schema_admits() -> None:
    """Tier 1: the two halves agree — what the model may say, the op takes.

    Asserted in this direction because the failure was a schema NARROWER than
    the op: a call the model cannot make is invisible from the op's side.
    """
    op = PresentIROp(
        kind="present",
        data_inline={"k": "v"},
        blueprint=_TWO_COMPONENT_BLUEPRINT,
    )

    assert op.blueprint == _TWO_COMPONENT_BLUEPRINT


def test_a_single_component_survives_as_a_one_element_list() -> None:
    """Tier 1: the narrowing loses no case.

    The object form is what ``array`` gives up, so this pins that its content is
    still expressible — otherwise the fix would have traded one unreachable
    shape for another.
    """
    single = [{"component": "text", "text": "just one"}]
    schema = _present_parameters()

    jsonschema.validate({"data_inline": {"k": "v"}, "blueprint": single}, schema)
    assert PresentIROp(
        kind="present", data_inline={"k": "v"}, blueprint=single
    ).blueprint == single
