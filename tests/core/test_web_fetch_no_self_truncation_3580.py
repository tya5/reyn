"""#3580 ③ — web_fetch stops truncating, and stops offering a page it cannot turn.

``web_fetch`` used to cap its extracted text at ``max_length`` (default 50,000
characters) and report ``truncated: true`` plus ``next_start: <offset>`` so the
model could resume via ``start_index``. Two things were measured about that:

- ``start_index`` was never in the tool schema (``git log -S start_index`` on
  ``tools/web_fetch.py`` is empty — never wired, not wired-then-removed), so the
  model could not resume. It was handed "there is more, resume at N" and no way
  to say N.
- ``web.py`` has no HTTP Range, no ETag, no cache. Had the argument existed, the
  next page would have been the whole page downloaded again and sliced at a
  different offset.

So the pair is gone, together with the ``max_length`` argument that produced it.
Nothing replaces it — owner ruling on #3580:
「わけわからんオレオレ仕様なんて廃止して」 (abolish the bespoke per-tool scheme)
and 「既存機能があるのに別機構を足すな」 (don't add a second mechanism when one
already exists). The surviving ceiling on what a fetch puts into the model's
context is the OS-level tool-result cap (``offload.enabled``, shipped ``false``),
which is unchanged by this file. Download volume is still bounded separately by
``web_fetch.max_download_bytes`` (10 MiB).

The tests below therefore assert the SEAM, not the absent field: a body far over
the old cap arrives whole from the handler, and then behaves like any other tool
result at ``ContextBudgetAdvisor.cap_tool_result`` — uncapped with offload off,
capped with offload on.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from reyn.config.chat import CompactionConfig, OffloadConfig
from reyn.core.offload.canonical import web_fetch_to_canonical
from reyn.core.op_runtime.web import handle_web_fetch
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.schemas.models import WebFetchIROp

# Far over the removed 50,000-character cap, so "returned whole" cannot be
# confused with "happened to fit".
_BIG_BODY = "reyn " * 40_000  # 200,000 characters


class _ResponseStreamCtx:
    def __init__(self, response: "httpx.Response") -> None:
        self._response = response

    async def __aenter__(self) -> "httpx.Response":
        return self._response

    async def __aexit__(self, *args: object) -> None:
        pass


class _PlainTextClient:
    """Stand-in for the transport only — the code under test is everything
    ABOVE the socket. Not a stand-in for any reyn collaborator: the handler,
    the canonical mapper, the MediaStore and the budget advisor are all real."""

    body: str = ""

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=type(self).body.encode("utf-8"),
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_PlainTextClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def stream(self, method: str, url: str) -> "_ResponseStreamCtx":
        return _ResponseStreamCtx(self._response)


class _RecordingEventLog:
    subscribers: list = []

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, name: str, **payload: Any) -> None:
        self.emitted.append((name, payload))


def _op_context(tmp_path: Path, events: Any) -> Any:
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    class _Workspace:
        pass

    return OpContext(
        workspace=_Workspace(),  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_fetch_config=None,
        media_store=MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="test-agent", session_id="test-session"),
    )


def _fetch_big(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Any) -> dict:
    _PlainTextClient.body = _BIG_BODY
    monkeypatch.setattr(httpx, "AsyncClient", _PlainTextClient)
    return asyncio.run(
        handle_web_fetch(
            op=WebFetchIROp(kind="web_fetch", url="https://example.com"),
            ctx=_op_context(tmp_path, events),
        )
    )


def _advisor(tmp_path: Path, *, offload_enabled: bool) -> ContextBudgetAdvisor:
    """A real ContextBudgetAdvisor. ``compaction_controller=None`` is a supported
    production shape — ``resolve_effective_trigger_and_budgets`` falls back to the
    model's max-input-tokens — so nothing here is a stand-in for the cap logic."""
    return ContextBudgetAdvisor(
        compaction=CompactionConfig(),
        compaction_controller=None,
        media_store=MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="test-agent", session_id="test-session"),
        model_fn=lambda: "gpt-4o",
        events=_RecordingEventLog(),
        history_fn=list,
        offload_config=OffloadConfig(enabled=offload_enabled),
    )


def test_a_body_far_over_the_old_cap_comes_back_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: web_fetch returns the extracted text in full, at 4x the removed cap.

    This is the gate for the removal itself. The old code returned
    ``content[:50_000]``; a 200,000-character body is unambiguous about which
    code is running. Strip-falsify: reinstate the slice in
    ``op_runtime/web.py`` and this goes red on the body-equality assertion.
    """
    result = _fetch_big(tmp_path, monkeypatch, _RecordingEventLog())

    assert result["status"] == "ok"
    assert result["content"] == _BIG_BODY


def test_the_result_no_longer_offers_a_page_it_cannot_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: neither the result payload nor the audit-event carries a resume handle.

    ``truncated``/``next_start``/``start_index`` were a standing offer the model
    could not accept — ``start_index`` was never a tool argument. Removing the
    truncation without removing the offer would keep paying tokens to advertise
    it, so both surfaces are checked here rather than the payload alone.
    """
    events = _RecordingEventLog()
    result = _fetch_big(tmp_path, monkeypatch, events)

    for field in ("truncated", "next_start", "start_index"):
        assert field not in result, f"{field} is still in the LLM-visible result"

    completed = [p for name, p in events.emitted if name == "web_fetch_completed"]
    assert completed, "web_fetch_completed was not emitted"
    for field in ("truncated", "next_start", "start_index"):
        assert field not in completed[0], f"{field} is still in the audit-event"


def test_the_canonical_the_model_reads_has_the_whole_page_and_no_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: the canonical mapper — the surface the model actually reads — carries
    the full body as ``text`` and no ``truncated``/``next_start`` frontmatter."""
    canonical = web_fetch_to_canonical(_fetch_big(tmp_path, monkeypatch, _RecordingEventLog()))

    assert canonical["text"] == _BIG_BODY
    assert "truncated" not in canonical["meta"]
    assert "next_start" not in canonical["meta"]


def test_a_stray_max_length_argument_is_ignored_rather_than_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a model still sending the removed argument gets a normal fetch.

    ``max_length`` was LLM-visible, so old prompts and prompt-cached transcripts
    can keep emitting it. Dispatch passes args unmodified with no schema
    validation (``tools/dispatch.invoke_tool``), and the handler reads only
    ``url`` — so the inertia case degrades to "the argument does nothing",
    never to a failed tool call.
    """
    from reyn.tools.web_fetch import WEB_FETCH

    _PlainTextClient.body = _BIG_BODY
    monkeypatch.setattr(httpx, "AsyncClient", _PlainTextClient)

    class _ToolCtx:
        workspace = None
        events = _RecordingEventLog()
        permission_resolver = None
        resolver = None
        router_state = None

    result = asyncio.run(
        WEB_FETCH.handler(
            {"url": "https://example.com", "max_length": 500}, _ToolCtx(),  # type: ignore[arg-type]
        )
    )
    assert result["status"] == "ok"
    assert result["content"] == _BIG_BODY, (
        "a removed argument must not silently re-acquire meaning"
    )


def test_offload_off_leaves_the_fetch_uncapped_at_the_real_chokepoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: with the shipped default the OS-level cap is a no-op, so the whole
    page reaches the model — the honest consequence of removing the per-tool cap,
    asserted at the chokepoint every tool result passes through rather than
    inferred from the handler's return."""
    canonical = web_fetch_to_canonical(_fetch_big(tmp_path, monkeypatch, _RecordingEventLog()))
    capped = _advisor(tmp_path, offload_enabled=False).cap_tool_result(canonical["text"])

    assert capped == _BIG_BODY


def test_offload_on_caps_the_fetch_at_the_real_chokepoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: opting in to ``offload.enabled`` is what bounds a large fetch now.

    Paired with the test above, this is the whole of ③'s size story: web_fetch
    owns no ceiling, and the one ceiling that exists is the operator's switch.
    """
    canonical = web_fetch_to_canonical(_fetch_big(tmp_path, monkeypatch, _RecordingEventLog()))
    capped = _advisor(tmp_path, offload_enabled=True).cap_tool_result(canonical["text"])

    assert len(capped) < len(_BIG_BODY), (
        "offload.enabled did not bound a 200,000-character tool result"
    )
