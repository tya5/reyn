"""#3580 — one offload path, and its sizes belong to the operator.

``web_fetch`` used to offload unconditionally (#385): whenever a MediaStore was
wired it stored the body and returned ``content: ""`` plus a structured preview.
That was a second offload mechanism alongside the generic tool-result cap, and
the two had reached OPPOSITE conclusions about the same defect — the generic one
ships DISABLED precisely because "the model often doesn't read the ref back and
acts on the preview instead", while web_fetch's did it on every fetch and left
no body inline at all.

So the special case is gone. Sizing is now one question with one answer: opt in
to ``offload.enabled`` and tune the bounds, or receive results whole.

The size bounds were module constants, which made "cap, but less aggressively"
unexpressible — the only lever was the boolean. They are config fields now, and
the tests below assert each one CHANGES SOMETHING rather than merely parsing:
a knob that reads back correctly and reaches nothing is the failure mode this
repo keeps finding.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from reyn.config.chat import _build_offload_config
from reyn.core.offload.canonical import web_fetch_to_canonical
from reyn.core.offload.seam import build_offload_body
from reyn.core.op_runtime.web import handle_web_fetch
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.services.tool_result_cap import (
    TRIGGER_CAP,
    cap_tool_result_content,
    compute_cap_tokens,
)
from reyn.schemas.models import WebFetchIROp
from reyn.services.turn_budget.engine import build_default_turn_budget_engine

_SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>Sample Article Title</title></head>
<body>
<h1>Top Heading</h1>
<p>The opening paragraph introduces the topic and outlines what the rest of the page will cover.</p>
<a href="https://a.example/">link a</a>
</body></html>
"""


class _ResponseStreamCtx:
    def __init__(self, response: "httpx.Response") -> None:
        self._response = response

    async def __aenter__(self) -> "httpx.Response":
        return self._response

    async def __aexit__(self, *args: object) -> None:
        pass


class _CapturingHtmlClient:
    body: str = ""
    content_type: str = "text/html"

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": type(self).content_type},
            content=type(self).body.encode("utf-8"),
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_CapturingHtmlClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def stream(self, method: str, url: str) -> "_ResponseStreamCtx":
        return _ResponseStreamCtx(self._response)


def _ctx_with_media_store(tmp_path: Path) -> Any:
    """A real OpContext with a real MediaStore rooted at ``tmp_path`` — the
    condition that used to trigger the removed offload."""
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    class _FakeEventLog:
        subscribers: list = []

        def emit(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _FakeWorkspace:
        pass

    return OpContext(
        workspace=_FakeWorkspace(),  # type: ignore[arg-type]
        events=_FakeEventLog(),  # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_fetch_config=None,
        media_store=MediaStore(MediaStoreConfig(), project_root=tmp_path, session_id="test-session"),
    )


def _fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    _CapturingHtmlClient.body = _SAMPLE_HTML
    _CapturingHtmlClient.content_type = "text/html"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHtmlClient)
    return asyncio.run(
        handle_web_fetch(
            op=WebFetchIROp(kind="web_fetch", url="https://example.com"),
            ctx=_ctx_with_media_store(tmp_path),
        )
    )


def test_a_wired_media_store_no_longer_diverts_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a fetch returns its body inline even with a MediaStore present.

    The store being wired was the entire trigger, so this is the condition the
    removed branch fired on — asserting it against a REAL MediaStore rather than
    its absence is what makes the test about the removal instead of about the
    legacy path that always behaved this way.
    """
    result = _fetch(tmp_path, monkeypatch)

    assert result["status"] == "ok"
    assert "opening paragraph introduces the topic" in result["content"]
    assert "path_ref" not in result
    assert "preview" not in result
    assert "stored_as" not in result


def test_the_llm_now_receives_prose_rather_than_a_dict_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: the canonical mapper turns the fetch into the page's text.

    This is the property that made the removal worth doing rather than merely
    tidy. The mapper reads ``content`` first and falls back to ``preview``, so
    while the body was diverted the tool result reaching the model was
    ``str(preview_dict)`` — a Python dict repr, braces and quotes included.
    """
    canonical = web_fetch_to_canonical(_fetch(tmp_path, monkeypatch))

    text = str(canonical.get("text") or "")
    assert "opening paragraph introduces the topic" in text
    assert not text.startswith("{"), f"the model is being handed a dict repr: {text[:80]!r}"


def test_each_size_field_parses_independently() -> None:
    """Tier 1: a config that sets one bound keeps the shipped value for the rest.

    An all-or-nothing parse would make tuning one number silently reset the
    others to whatever the parser defaulted to.
    """
    shipped = _build_offload_config({})
    partial = _build_offload_config({"enabled": True, "cap_ceil_tokens": 512})

    assert partial.cap_ceil_tokens == 512
    assert partial.max_inline_bytes == shipped.max_inline_bytes
    assert partial.structured_inline_max_chars == shipped.structured_inline_max_chars


def test_the_token_cap_follows_its_config() -> None:
    """Tier 2: ``cap_ceil_tokens`` / ``cap_alpha`` reach the computed cap."""
    trigger = 100_000
    shipped = compute_cap_tokens(trigger)
    tuned = compute_cap_tokens(trigger, ceil_tokens=512, alpha=0.1)

    assert tuned < shipped, (
        f"a lower ceiling did not lower the cap ({tuned} vs {shipped})"
    )


def test_the_inline_preview_size_follows_its_config() -> None:
    """Tier 2: ``preview_head_chars`` reaches the text the model is left with."""
    body = "H" * 20_000 + "T" * 20_000

    def _save(_content: str, **_kwargs: Any) -> dict:
        return {"path": ".reyn/tool-results/x.txt"}

    shipped = cap_tool_result_content(
        body, cap_tokens=100, model="openai/gpt-4o", save_fn=_save, trigger=TRIGGER_CAP,
    )
    tuned = cap_tool_result_content(
        body, cap_tokens=100, model="openai/gpt-4o", save_fn=_save, trigger=TRIGGER_CAP,
        preview_head_chars=100, preview_tail_chars=50,
    )

    assert tuned.count("H") < shipped.count("H"), (
        "a smaller head budget did not shrink the head kept inline"
    )


def test_the_structured_gate_follows_its_config() -> None:
    """Tier 2: ``structured_inline_max_chars`` decides when a dict gets its own ref."""
    canonical = {
        "attachments": [{"kind": "structured", "data": {"k": "v" * 200}}],
        "meta": {},
        "text": "t",
    }

    def _save(_serialized: str, **_kwargs: Any) -> dict:
        return {"path": ".reyn/tool-results/s.json"}

    shipped, *_ = build_offload_body(canonical, save_fn=_save, enabled=True)
    tuned, *_ = build_offload_body(
        canonical, save_fn=_save, enabled=True, structured_inline_max_chars=50
    )

    assert "structured_ref" not in shipped
    assert "structured_ref" in tuned, (
        "lowering the structured gate did not push the payload to its own ref"
    )


def test_the_force_close_reserve_follows_its_config() -> None:
    """Tier 2: ``max_inline_bytes`` reaches the turn budget's layer-1 threshold.

    The ceiling is not only a preview bound — it is what the budget reserves for
    "one more increment", so an operator who changes it changes when force-close
    fires. Asserted on the threshold rather than on the reserve, because the
    threshold is the value that decides anything.
    """
    shipped = build_default_turn_budget_engine("openai/gpt-4o")
    tuned = build_default_turn_budget_engine("openai/gpt-4o", max_inline_bytes=4_096)

    # A content size that force-closes under the shipped reserve. Derived from
    # the engine rather than written as a literal, so the test states the
    # RELATION (a smaller reserve tolerates more content) instead of pinning a
    # model-specific number that would drift with any window change.
    at_shipped_limit = shipped.budget.force_close_threshold

    assert shipped.should_force_close(at_shipped_limit), "setup: not at the shipped limit"
    assert not tuned.should_force_close(at_shipped_limit), (
        "reserving a smaller increment did not let the turn run longer — "
        "max_inline_bytes is not reaching the force-close decision"
    )


# ── the shipped defaults are a three-surface contract ──────────────────────
#
# Everything above asserts that a field CHANGES something. None of it pins what
# the shipped value IS — measured by breaking `max_inline_bytes` from 16384 to
# 1: all seven tests above stayed green, as did the config-mirror gate (it
# checks fields are DOCUMENTED, not that the numbers agree) and the two
# pre-existing suites that touch the constant. Nothing in the repo noticed.
#
# That matters because `max_inline_bytes` feeds the turn budget's force-close
# reserve, so a silently-changed default moves when force-close fires — while
# the docs and the example config keep advertising the old number.
#
# The numbers live HERE, and all three surfaces are compared against them. An
# equality between two of the surfaces would be blind to both drifting the same
# way, which is the shape this whole block exists to close.

_SHIPPED_DEFAULTS: "dict[str, float]" = {
    "max_inline_bytes": 16_384,
    "preview_head_chars": 6_000,
    "preview_tail_chars": 2_000,
    "cap_ceil_tokens": 4_096,
    "cap_alpha": 0.5,
    "structured_inline_max_chars": 2_000,
    "structured_preview_chars": 600,
}


def test_the_code_ships_the_documented_defaults() -> None:
    """Tier 1: surface 1 of 3 — the dataclass."""
    shipped = _build_offload_config({})

    actual = {name: getattr(shipped, name) for name in _SHIPPED_DEFAULTS}
    assert actual == _SHIPPED_DEFAULTS


@pytest.mark.repo_root_cwd(
    reason="reads docs/reference/config/reyn-yaml.md by a repo-relative "
    "path — a committed doc, not a per-test fixture; needs cwd == the real "
    "repo root, not the #3705 autouse isolated tmp_path.",
)
def test_the_reference_documents_the_shipped_defaults() -> None:
    """Tier 1: surface 2 of 3 — `reyn.yaml`'s field table.

    An operator reads the table to decide whether they need to set anything at
    all, so a table that disagrees with the code is worse than no table.
    """
    import re
    from pathlib import Path

    table = Path("docs/reference/config/reyn-yaml.md").read_text(encoding="utf-8")
    for name, expected in _SHIPPED_DEFAULTS.items():
        # 1+ plain-word columns (Axis, Type, ...) may sit between the Field
        # cell and the backtick-quoted Default cell -- #4206 added an "Axis"
        # column to this table, so the column count is no longer fixed at
        # exactly one (Type). Match the Default cell positionally by its own
        # shape (a backtick-quoted number) rather than pinning a column count.
        row = re.search(rf"\| `{re.escape(name)}` \|(?:\s*\w+\s*\|)+\s*`([0-9.]+)`\s*\|", table)
        assert row is not None, f"{name} has no row in reyn-yaml.md's offload table"
        assert float(row.group(1)) == float(expected), (
            f"reyn-yaml.md documents {name}={row.group(1)}, code ships {expected}"
        )


@pytest.mark.repo_root_cwd(
    reason="reads reyn.local.yaml.example by a repo-relative path — a "
    "committed file, not a per-test fixture; needs cwd == the real repo "
    "root, not the #3705 autouse isolated tmp_path.",
)
def test_the_example_config_shows_the_shipped_defaults() -> None:
    """Tier 1: surface 3 of 3 — `reyn.local.yaml.example`.

    The example is what an operator copies, so a stale number there is one they
    paste into their own config believing it is the default.
    """
    import re
    from pathlib import Path

    example = Path("reyn.local.yaml.example").read_text(encoding="utf-8")
    for name, expected in _SHIPPED_DEFAULTS.items():
        line = re.search(rf"^#\s+{re.escape(name)}:\s*([0-9.]+)", example, re.M)
        assert line is not None, f"{name} is not shown in reyn.local.yaml.example"
        assert float(line.group(1)) == float(expected), (
            f"the example shows {name}={line.group(1)}, code ships {expected}"
        )
