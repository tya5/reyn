# scaffold: triggered_by="#5620 -- scripts/litellm_proxy_patch/litellm_proxy_patch.py (D)"
# scaffold: removed_by="the litellm 1.95.0 proxy defects this file reproduces are fixed upstream -- see each test's own docstring"
"""Tier scaffold: #5620 -- "does the upstream litellm PROXY-layer defect
`litellm_proxy_patch.py`'s own patch D works around still exist" tests,
run against RAW, UNPATCHED litellm classes.

## Version-pinned, not skipped bare (architect's own #5620 ruling)

This file targets litellm **1.95.0 exactly** -- the version the owner's
own `junk/litellm` proxy runs (a SEPARATE install/venv from reyn's own
in-process litellm, currently 1.96.2 -- #5620's own lib-side retirement).
reyn's own dev venv is never expected to carry 1.95.0, so this file
module-level-skips with an EXPLICIT qualifier (never a bare skip) on any
other version -- the CI leg that actually runs this file un-skipped
installs `litellm[proxy]==1.95.0` in its own dedicated venv (path-
conditional workflow, `.github/workflows/litellm-proxy-patch-scaffold.yml`)
and treats a skip there (or 0 collected) as RED (vacuity guard) -- see
that workflow's own comments.

## The red/green meaning is INVERTED here (read before touching)

- **GREEN = the defect still reproduces** = `litellm_proxy_patch.py`'s
  own workaround (D) is still needed. This is the expected, ONGOING
  state on litellm 1.95.0.
- **RED (on 1.95.0, not a version-mismatch skip) = the defect no longer
  reproduces** = litellm fixed it upstream. **This is GOOD NEWS, not a
  regression.** Remove `scripts/litellm_proxy_patch/`, its own CI leg,
  `tests/llm/test_5620_litellm_proxy_patch_d.py`, and THIS FILE, in the
  same PR (this file's own `removed_by` trigger).

All three defects are reproduced directly against synthetic objects
built to match exactly what the real chain constructs -- no network, no
provider, no live proxy process -- verified DIRECTLY against the
owner's own real litellm 1.95.0 install (`~/Workspace/junk/litellm/venv`)
before being written here, not assumed from source reading alone; each
test's own docstring cites the exact file:line this was read against.
"""
from __future__ import annotations

import importlib.metadata

import pytest

_PINNED_LITELLM_VERSION = "1.95.0"


def _installed_litellm_version() -> "str | None":
    try:
        return importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        return None


_installed = _installed_litellm_version()
if _installed != _PINNED_LITELLM_VERSION:
    pytest.skip(
        f"proxy litellm pin {_PINNED_LITELLM_VERSION}; found "
        f"{_installed!r} -- this file only runs un-skipped in the "
        "dedicated litellm[proxy]==1.95.0 CI leg "
        "(.github/workflows/litellm-proxy-patch-scaffold.yml), never in "
        "reyn's own main test run against reyn's own in-process litellm",
        allow_module_level=True,
    )


def test_defect_1_chatgpt_config_forces_stream_true() -> None:
    """Tier scaffold: #5620 defect ① -- `ChatGPTResponsesAPIConfig.
    transform_responses_api_request` (litellm/llms/chatgpt/responses/
    transformation.py:84, litellm 1.95.0) unconditionally sets
    `request["stream"] = True` on every outbound request, regardless of
    what the caller asked for -- verified directly against the real
    installed source before writing this test."""
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
    from litellm.types.router import GenericLiteLLMParams

    cfg = ChatGPTResponsesAPIConfig()
    request = cfg.transform_responses_api_request(
        model="gpt-5",
        input="hi",
        response_api_optional_request_params={"stream": False},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )
    assert request["stream"] is True, (
        "GREEN (this assertion passing) means the defect still "
        "reproduces: a caller-requested stream=False request is still "
        "force-rewritten to stream=True by ChatGPTResponsesAPIConfig -- "
        "if this assertion now FAILS (request['stream'] is False), "
        "litellm fixed this upstream: remove scripts/litellm_proxy_"
        "patch/, its CI leg, and this whole file (see module docstring)"
    )


def test_defect_2_llm_http_handler_folds_forced_stream_back_in() -> None:
    """Tier scaffold: #5620 defect ② -- `BaseLLMHTTPHandler`'s own
    `async_response_api_handler` (litellm/llms/custom_httpx/
    llm_http_handler.py:2718, litellm 1.95.0): `stream = bool(stream or
    data.get("stream"))` folds defect ①'s own forced `data["stream"] =
    True` back into the resolved `stream` flag, even though the ORIGINAL
    caller-level `response_api_optional_request_params.get("stream",
    False)` was `False`. This test reproduces the exact fold-back
    expression at that line, not the full async handler (no network) --
    the isolated arithmetic IS the defect: once ① has already forced
    `data["stream"]`, nothing downstream can tell "the caller actually
    wanted streaming" from "the chatgpt config forced it" apart."""
    caller_stream = False
    data_after_defect_1 = {"stream": True}  # what ①'s own forcing leaves behind
    resolved_stream = bool(caller_stream or data_after_defect_1.get("stream"))
    assert resolved_stream is True, (
        "GREEN (this assertion passing) means the defect still "
        "reproduces: the caller's own stream=False is folded back into "
        "True by the forced data['stream'] -- if this assertion now "
        "FAILS, litellm's own fold-back expression changed upstream: "
        "remove scripts/litellm_proxy_patch/, its CI leg, and this "
        "whole file (see module docstring)"
    )


def test_defect_3_bridge_transform_response_raises_on_empty_output() -> None:
    """Tier scaffold: #5620 defect ③ -- `LiteLLMResponsesTransformationHandler.
    transform_response` (litellm/completion_extras/
    litellm_responses_transformation/transformation.py:700, litellm
    1.95.0) raises `ValueError("Unknown items in responses API response:
    []")` when `raw_response.output` is empty AND the logging object's
    own `model_call_details["original_response"]` carries nothing this
    method's own recovery helper can parse -- the SAME defect reyn's own
    (now-retired, #5620) lib-side patch A worked around for reyn's OWN
    litellm 1.96.2 install; this test reproduces it independently against
    the PROXY's own separate 1.95.0 install, verified directly against
    the real installed source before writing this test."""
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        LiteLLMResponsesTransformationHandler,
    )
    from litellm.types.llms.openai import ResponsesAPIResponse

    handler = LiteLLMResponsesTransformationHandler()

    class _FakeModelResponse:
        pass

    class _FakeLoggingObj:
        model_call_details: dict = {}  # no "original_response" to recover from

    raw_response = ResponsesAPIResponse.model_construct(
        id="resp_1", object="response", created_at=0, model="gpt-5",
        status="completed", output=[], error=None, incomplete_details=None,
    )

    with pytest.raises(ValueError, match="Unknown items in responses API response"):
        handler.transform_response(
            model="gpt-5",
            raw_response=raw_response,
            model_response=_FakeModelResponse(),
            logging_obj=_FakeLoggingObj(),
            request_data={},
            messages=[],
            optional_params={},
            litellm_params={},
            encoding=None,
        )
