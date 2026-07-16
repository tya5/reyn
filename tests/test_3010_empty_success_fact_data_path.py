"""A legitimately-empty tool success is readable as a FACT, not only as display prose (#3010).

The invariant under test is a seam rule of the canonical layer, not a bug repro: a canonical result
is consumed by TWO paths with different needs, and an empty success must be legible to both.

- The CHAT path reads a rendering. A blank tool body is bad LLM UX, so a legit-empty success renders
  as an explicit marker ("(no content)" / "(no output)" / "(empty file)"). That is prose, chosen for
  a reader.
- The DATA path (a pipeline `tool:` step, via ``canonical_to_ctx_fields``) reads values. Handed only
  the marker, it cannot distinguish "the document contained the text '(no content)'" from "the
  document was empty" -- and it must never have to string-match display prose to find out, because
  that binds a data consumer to today's chat wording.

An empty success is a TRUE state, NOT an error: a scanned PDF with no text layer converts perfectly,
and "there is no text here" is the converter's honest report. So the fact rides ``meta.empty``, a
sibling of ``meta.isError`` -- never ``isError`` itself, which would be a lie about a success.

These pin the invariant in both directions: the fact is present for the data path, AND the chat
rendering is unchanged by its presence (the marker still IS the body; the fact is not echoed into
the frontmatter, where it would only restate what the body already says).
"""
from __future__ import annotations

import pytest

from reyn.core.offload.canonical import canonical_to_ctx_fields, to_canonical
from reyn.core.offload.seam import build_offload_body, render_tool_result
from reyn.tools import get_default_registry

# Canonical declarations are born at the REGISTRATION seam (FP-0056 PR-F1), so the registry must be
# built before ``to_canonical`` can dispatch on an invoked identity -- otherwise every case below
# silently takes the whole-dict fallback and asserts nothing about its mapper (observed while
# writing these: all 10 cases failed on the fallback shape, not on the invariant).
get_default_registry()

# (source, empty-success result, the marker that success renders as) for the producers whose success
# path can legitimately yield no body. Driven through the REAL ``to_canonical`` dispatch (the invoked
# identity), not by reaching for a private mapper.
#
# Deliberately NOT MCP-only. Whether the MCP mappers reach ``_explicit_empty`` at all depends on the
# third-party server's SDK version: a server on a ``structuredContent``-era SDK returns
# ``{"content": "", "structuredContent": {"result": ""}}``, which reyn carries as a structured
# attachment -- and the mapper skips the marker when an attachment is present. The producers below
# that never touch MCP (``read_file``/``shell``/``sandboxed_exec``/``render_template``) call
# ``_explicit_empty`` directly, so they pin this invariant independently of any SDK version.
_EMPTY_SUCCESS_CASES = [
    # --- non-MCP: reached today, on every SDK ---
    ("read_file", {"op": "read", "status": "ok", "content": ""}, "(empty file)"),
    ("shell", {"status": "ok", "data": ""}, "(no output)"),
    ("sandboxed_exec", {"stdout": "", "stderr": "", "returncode": 0}, "(no output)"),
    ("render_template", {"rendered": ""}, "(empty render)"),
    ("read_memory_body", {"content": ""}, "(empty)"),
    ("run_pipeline", {"output": ""}, "(no output)"),
    # A pipeline that finished with no output at all -- the branch that hardcoded its marker
    # instead of calling the helper, and so was invisible to a grep for it.
    ("run_pipeline", {"output": None}, "(no output)"),
    # --- MCP: reachable when the server emits no structuredContent ---
    ("call_mcp_tool", {"content": ""}, "(no content)"),
    ("read_mcp_resource", {"contents": []}, "(no content)"),
    ("web_fetch", {"content": "", "preview": ""}, "(no content)"),
]


@pytest.mark.parametrize("source,result,marker", _EMPTY_SUCCESS_CASES)
def test_empty_success_carries_the_fact_to_the_data_path(
    source: str, result: dict, marker: str,
) -> None:
    """Tier 2b: an empty SUCCESS reaches a pipeline step as a readable fact (``meta.empty``).

    This is what a data-path consumer reads INSTEAD of matching the marker string.
    """
    ctx_fields = canonical_to_ctx_fields(to_canonical(result, source=source))

    assert ctx_fields.get("meta", {}).get("empty") is True, (
        f"{source}: an empty success must be a readable fact on the data path -- otherwise the only "
        f"evidence it happened is the prose {marker!r}, which a consumer would have to string-match"
    )
    # ...and it is NOT an error: the producer succeeded. Flagging it would be a false report, and
    # would route a legitimate empty down an error path.
    assert "isError" not in ctx_fields.get("meta", {}), (
        f"{source}: an empty success is not an error -- the conversion/command succeeded"
    )


@pytest.mark.parametrize("source,result,marker", _EMPTY_SUCCESS_CASES)
def test_empty_success_still_renders_its_marker_to_the_llm(
    source: str, result: dict, marker: str,
) -> None:
    """Tier 2b: the LLM-visible BODY is untouched -- ``text`` stays the marker, byte for byte.

    Recording the fact may ADD a frontmatter signal (``empty`` renders, like ``returncode`` and
    unlike ``isError``), but it must never rewrite the body: a data-path fix that changed what the
    chat LLM reads would be a regression in the path that was working.
    """
    canonical = to_canonical(result, source=source)
    frontmatter, text, _media, _content_type = build_offload_body(canonical)

    assert text == marker, f"{source}: the LLM must still see the explicit marker, not a blank body"
    assert frontmatter.get("empty") is True, (
        f"{source}: the fact is an ordinary signal -- the LLM reads it alongside the marker"
    )
    # The fact reaches the LLM as a frontmatter line; the marker survives verbatim as the body.
    rendered = render_tool_result(frontmatter, text)
    assert rendered.endswith(marker)
    assert "empty: true" in rendered


def test_non_empty_success_is_not_flagged_empty() -> None:
    """Tier 2b: falsify direction -- the fact tracks emptiness, it is not stamped unconditionally.

    Without this, a mapper that always set the flag would pass every assertion above while telling
    the data path that every document is empty.
    """
    ctx_fields = canonical_to_ctx_fields(
        to_canonical({"content": "real document text"}, source="call_mcp_tool"),
    )

    assert ctx_fields["text"] == "real document text"
    assert "empty" not in ctx_fields.get("meta", {})


def test_a_document_whose_text_is_the_marker_is_distinguishable_from_an_empty_one() -> None:
    """Tier 2b: the fact -- not the prose -- is what separates the two.

    This is the case a string-matching consumer gets wrong, and the reason the fact must exist: both
    results render the identical body, so ``text`` alone cannot tell them apart. A document that
    genuinely contains "(no content)" is real content and must be indexed; an empty one must not.
    """
    real_doc = canonical_to_ctx_fields(
        to_canonical({"content": "(no content)"}, source="call_mcp_tool"),
    )
    empty_doc = canonical_to_ctx_fields(to_canonical({"content": ""}, source="call_mcp_tool"))

    assert real_doc["text"] == empty_doc["text"], (
        "precondition: the two are indistinguishable by text -- that is the whole problem"
    )
    assert "empty" not in real_doc.get("meta", {}), "a real document is not empty"
    assert empty_doc.get("meta", {}).get("empty") is True
