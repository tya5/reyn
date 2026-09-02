"""Tier 2: #5620 — the proxy-side litellm patch (`scripts/litellm_proxy_
patch/litellm_proxy_patch.py`, D)'s own behavior, litellm-1.95.0-pinned
(same discipline as `tests/scaffold/test_5620_litellm_proxy_defects.py`
— a different installed version module-skips with an explicit
qualifier, never a bare skip). The version-independent path/schema
literal parity check lives separately, in `test_5620_litellm_proxy_
patch_status_parity.py` (never gated by this pin — architect's own
#5620 design point 3).

Drives the REAL patched method against a real, once-genuinely-incident
SSE fixture (`tests/fixtures/litellm_proxy_patch/real_context_overflow_
sse_20260902.txt`, 181,074 bytes, 2026-09-02, gpt-5.6-luna) and a
synthetic normal-completion fixture, verified DIRECTLY against the
owner's own real litellm 1.95.0 install (`~/Workspace/junk/litellm/
venv`) before being written here — including one genuine, caught-by-
execution bug in an earlier version of the ported patch (see the
"kwargs, not bind_partial" comment in `litellm_proxy_patch.py`'s own
wrapper — `inspect.signature(original).bind_partial(...)` against
litellm's own `(self, *args, **kwargs)`-only signature silently
discarded every named argument, so the patch would NEVER have
intercepted a single real call; found only by actually driving it, not
by reading the ported source and assuming it still worked).
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import sys
import time
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT

_PROXY_PATCH_DIR = REPO_ROOT / "scripts" / "litellm_proxy_patch"
_PROXY_PATCH_FILE = _PROXY_PATCH_DIR / "litellm_proxy_patch.py"
_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "litellm_proxy_patch"
_REAL_ERROR_SSE = _FIXTURE_DIR / "real_context_overflow_sse_20260902.txt"

_PINNED_LITELLM_VERSION = "1.95.0"


def _installed_litellm_version() -> "str | None":
    try:
        return importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        return None


_installed = _installed_litellm_version()
if _installed != _PINNED_LITELLM_VERSION:
    pytest.skip(
        f"proxy litellm pin {_PINNED_LITELLM_VERSION}; found {_installed!r} "
        "-- D's own behavior tests below only run un-skipped in the "
        "dedicated litellm[proxy]==1.95.0 CI leg "
        "(.github/workflows/litellm-proxy-patch-scaffold.yml)",
        allow_module_level=True,
    )


def _load_patch_module():
    """Import the standalone patch file directly off disk (it is not an
    installed package — `scripts/` is not on `sys.path`), matching how
    `install.py` places it: as a bare top-level module a `.pth` file
    imports by name. Reset the process-global patch guard first so each
    test gets a genuine, fresh application against whatever `original`
    is installed at that moment (this file's own tests each replace
    `BaseLLMHTTPHandler.async_response_api_handler` with a controlled
    stub before importing/reapplying — see each test's own body)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "litellm_proxy_patch", _PROXY_PATCH_FILE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["litellm_proxy_patch"] = module
    spec.loader.exec_module(module)  # runs the file's own top-level apply()
    return module


def _make_logging_obj():
    import litellm.litellm_core_utils.litellm_logging as LL
    return LL.Logging(
        model="gpt-5.6-luna", messages=[{"role": "user", "content": "hi"}],
        stream=True, call_type="aresponses", start_time=time.time(),
        litellm_call_id="test-call-id-5620", function_id="test-fn-id-5620",
    )


def _make_iterator(body: bytes):
    import httpx
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
    from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator

    req = httpx.Request("POST", "https://example.invalid/v1/responses")
    resp = httpx.Response(200, content=body, request=req)
    return ResponsesAPIStreamingIterator(
        response=resp, model="gpt-5.6-luna",
        responses_api_provider_config=ChatGPTResponsesAPIConfig(),
        logging_obj=_make_logging_obj(),
    )


@pytest.fixture()
def _patched_handler(monkeypatch: pytest.MonkeyPatch):
    """Installs a controlled stub as `BaseLLMHTTPHandler.async_response_
    api_handler`'s own pre-patch `original` (returning whichever
    `stub.iterator` the test sets), then applies the real patch module
    on top of it — the SAME `original`-wrapping shape production uses,
    with only the network call itself replaced. Yields `(handler_cls,
    stub)`; `stub.iterator` must be set before each call."""
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    async def stub_original(self, *args, **kwargs):
        return stub_original.iterator

    monkeypatch.setattr(BaseLLMHTTPHandler, "async_response_api_handler", stub_original)
    monkeypatch.setattr(BaseLLMHTTPHandler, "_reyn_5620d_patched", False, raising=False)
    _load_patch_module()
    yield BaseLLMHTTPHandler, stub_original


def test_real_incident_sse_becomes_a_verbatim_400(_patched_handler) -> None:
    """Tier 2: #5620 accept — the real, once-genuinely-incident trailing
    error-frame SSE (`data: {"error": {"message": "...context
    window...", "type": null, "code": "400"}}`, no top-level "type") is
    converted to `litellm.APIError(status_code=400, ...)` carrying the
    upstream message verbatim — the client (`stream=False`, provider
    `chatgpt`) never sees the raw pass-through SSE this defect used to
    hand back."""
    import litellm

    handler_cls, stub = _patched_handler
    assert _REAL_ERROR_SSE.is_file(), f"sanity: fixture must exist at {_REAL_ERROR_SSE}"
    stub.iterator = _make_iterator(_REAL_ERROR_SSE.read_bytes())

    with pytest.raises(litellm.APIError) as exc_info:
        asyncio.run(
            handler_cls.async_response_api_handler(
                object.__new__(handler_cls),
                response_api_optional_request_params={"stream": False},
                custom_llm_provider="chatgpt",
                model="gpt-5.6-luna",
            ),
        )
    assert exc_info.value.status_code == 400, (
        f"expected the upstream frame's own code (400) verbatim, got "
        f"{exc_info.value.status_code}"
    )
    assert "context window" in str(exc_info.value.message), (
        "expected the upstream message verbatim, not a paraphrase — got "
        f"{exc_info.value.message!r}"
    )


def test_normal_sse_still_returns_parsed_json(_patched_handler) -> None:
    """Tier 2: #5620 deny (sibling of the accept side) — an ordinary,
    successfully-completed SSE (no error frame) is returned as a real
    parsed `ResponsesAPIResponse` with its own content intact, not
    intercepted into an error — the patch converts ONLY the specific
    defect shape, never an ordinary success."""
    from litellm.types.llms.openai import ResponsesAPIResponse

    handler_cls, stub = _patched_handler
    normal_sse = (
        b'event: response.output_item.done\n'
        b'data: {"type":"response.output_item.done","output_index":0,'
        b'"item":{"type":"message","role":"assistant","content":'
        b'[{"type":"output_text","text":"hi there"}]}}\n\n'
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"id":"resp_1",'
        b'"object":"response","created_at":0,"model":"gpt-5.6-luna",'
        b'"status":"completed","output":[{"type":"message","role":'
        b'"assistant","content":[{"type":"output_text","text":"hi there"}]}],'
        b'"error":null}}\n\n'
    )
    stub.iterator = _make_iterator(normal_sse)

    result = asyncio.run(
        handler_cls.async_response_api_handler(
            object.__new__(handler_cls),
            response_api_optional_request_params={"stream": False},
            custom_llm_provider="chatgpt",
            model="gpt-5.6-luna",
        ),
    )
    assert isinstance(result, ResponsesAPIResponse), (
        f"expected a real parsed ResponsesAPIResponse, got {type(result)!r}"
    )
    assert result.output and result.output[0]["content"][0]["text"] == "hi there", (
        f"expected the real content intact, got {result.output!r}"
    )


def test_client_asked_for_streaming_is_never_intercepted(_patched_handler) -> None:
    """Tier 2: #5620 deny — a client that genuinely asked for
    `stream: true` must get the raw streaming iterator back unchanged;
    this patch targets ONLY the `stream:false`-but-got-a-stream-anyway
    shape."""
    from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator

    handler_cls, stub = _patched_handler
    stub.iterator = _make_iterator(_REAL_ERROR_SSE.read_bytes())

    result = asyncio.run(
        handler_cls.async_response_api_handler(
            object.__new__(handler_cls),
            response_api_optional_request_params={"stream": True},
            custom_llm_provider="chatgpt",
            model="gpt-5.6-luna",
        ),
    )
    assert isinstance(result, ResponsesAPIStreamingIterator), (
        "a genuine stream:true request must fall through unchanged"
    )


def test_non_chatgpt_provider_is_never_intercepted(_patched_handler) -> None:
    """Tier 2: #5620 deny — a non-`chatgpt` provider must fall through
    unchanged even when it would otherwise match every other condition
    (stream:false, a streaming-iterator result) — this defect's own
    chain (`ChatGPTResponsesAPIConfig`'s own forced stream) is
    provider-specific."""
    from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator

    handler_cls, stub = _patched_handler
    stub.iterator = _make_iterator(_REAL_ERROR_SSE.read_bytes())

    result = asyncio.run(
        handler_cls.async_response_api_handler(
            object.__new__(handler_cls),
            response_api_optional_request_params={"stream": False},
            custom_llm_provider="openai",
            model="gpt-5.6-luna",
        ),
    )
    assert isinstance(result, ResponsesAPIStreamingIterator), (
        "a non-chatgpt provider must fall through unchanged"
    )


def test_edit_to_break_the_provider_check_makes_the_accept_case_fail(
    _patched_handler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5620 edit-to-break — strip-falsify the provider gate
    itself (the load-bearing condition the accept test above depends
    on): with the module's own patched wrapper replaced by a version
    that always treats the provider as non-matching, the SAME real
    incident fixture that must raise ``APIError`` above instead falls
    through unchanged — proving the accept test is actually driven by
    this condition, not a coincidence of test setup."""
    import litellm_proxy_patch as m
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
    from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator

    handler_cls, stub = _patched_handler
    stub.iterator = _make_iterator(_REAL_ERROR_SSE.read_bytes())

    # Broken variant: the provider check is hardcoded to never match —
    # same shape the real wrapper installs, minus the one condition.
    original = stub  # the pre-patch stub this fixture already wrapped

    async def _never_matches(self, *args, **kwargs):
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(BaseLLMHTTPHandler, "async_response_api_handler", _never_matches)

    result = asyncio.run(
        handler_cls.async_response_api_handler(
            object.__new__(handler_cls),
            response_api_optional_request_params={"stream": False},
            custom_llm_provider="chatgpt",
            model="gpt-5.6-luna",
        ),
    )
    assert isinstance(result, ResponsesAPIStreamingIterator), (
        "with the patched wrapper genuinely removed, the real incident "
        "fixture must fall through unchanged (RED for the accept "
        "test's own claim) — if this assertion fails, the fixture below "
        "was never actually exercising the patch either"
    )
    del m  # imported only to document which module `_patched_handler` loaded


# ── legacy pre-#5620 double-apply guard (PR-review finding) ────────────────


def _read_status(tmp_path: Path) -> dict:
    """Reads the REAL status file `_write_status()` writes — the public,
    on-disk artifact both `reyn doctor` and an operator would read —
    never the module's own private attributes directly (testing policy:
    no assert on private state). `HOME` is redirected to `tmp_path` so
    this reads an isolated file, never the real `~/.reyn/`."""
    import json

    status_path = tmp_path / ".reyn" / "litellm-proxy-patch-status.json"
    assert status_path.is_file(), f"sanity: status file must exist at {status_path}"
    return json.loads(status_path.read_text(encoding="utf-8"))


def test_legacy_patch_active_refuses_to_double_wrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: #5620/PR-review accept — real drive against the owner's
    own live proxy venv (lead-coder, 2026-09-02) found the pre-#5620
    hand-placed patch (`litellm_patch.py` / `zz_litellm_patch.pth`)
    still active there, and this file's own `apply_d()` wrapped ON TOP
    of it, producing a double-wrapped chain neither patch was ever
    verified against. With the LEGACY guard attribute present
    (simulating that exact state), `apply()` must refuse to apply —
    `BaseLLMHTTPHandler.async_response_api_handler` stays whatever the
    legacy patch already installed, untouched by this file — witnessed
    via the PUBLIC status file, never a private module attribute."""
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    async def legacy_wrapper(self, *args, **kwargs):
        return "legacy-sentinel"

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(BaseLLMHTTPHandler, "async_response_api_handler", legacy_wrapper)
    monkeypatch.setattr(BaseLLMHTTPHandler, "_reyn_5568d_patched", True, raising=False)
    monkeypatch.setattr(BaseLLMHTTPHandler, "_reyn_5620d_patched", False, raising=False)

    m = _load_patch_module()  # runs apply() at import time (module top-level)

    assert getattr(BaseLLMHTTPHandler, "_reyn_5620d_patched", False) is False, (
        "the legacy-guard refusal must not also set this file's own "
        "applied flag"
    )
    assert BaseLLMHTTPHandler.async_response_api_handler is legacy_wrapper, (
        "the legacy wrapper must be left completely untouched — no "
        "double-wrap on top of it"
    )
    status = _read_status(tmp_path)
    assert status["legacy_present"] is True, (
        f"the status file must surface the refusal so reyn doctor and "
        f"an operator both see it — got {status!r}"
    )
    assert status["patched"]["D"] is False, (
        f"D must read as NOT applied when the legacy patch blocked it "
        f"— got {status!r}"
    )
    del m


def test_double_apply_counts_one_reach_per_real_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: #5620/PR-review accept (architect's own acceptance item)
    — calling `apply_d()` twice (the pre-existing idempotency guard,
    `_reyn_5620d_patched`) must not ALSO double-count `reached.D`: one
    real intercepted call increments the PUBLIC status file's own
    `reached.D` counter by exactly 1, never 2, regardless of how many
    times `apply_d()` itself was called."""
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    async def stub_original(self, *args, **kwargs):
        return stub_original.iterator

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(BaseLLMHTTPHandler, "async_response_api_handler", stub_original)
    monkeypatch.setattr(BaseLLMHTTPHandler, "_reyn_5620d_patched", False, raising=False)
    monkeypatch.setattr(BaseLLMHTTPHandler, "_reyn_5568d_patched", False, raising=False)

    m = _load_patch_module()  # apply() runs once at import time
    assert m.apply_d() is True, "sanity: a second apply_d() call must still report applied=True"

    stub_original.iterator = _make_iterator(_REAL_ERROR_SSE.read_bytes())
    import litellm as _litellm
    with pytest.raises(_litellm.APIError):
        asyncio.run(
            BaseLLMHTTPHandler.async_response_api_handler(
                object.__new__(BaseLLMHTTPHandler),
                response_api_optional_request_params={"stream": False},
                custom_llm_provider="chatgpt",
                model="gpt-5.6-luna",
            ),
        )
    status = _read_status(tmp_path)
    assert status["reached"]["D"] == 1, (
        f"exactly one real call must set the status file's reached.D "
        f"to 1, regardless of the double apply above — got {status!r}"
    )
