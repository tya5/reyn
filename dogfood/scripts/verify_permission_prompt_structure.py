"""Dogfood verify: production permission-prompt structure post-#163.

Goal: confirm that the actual require_web_fetch path (production code,
not a stub UserIntervention) produces an OutboxMessage with the
structured ``meta.prompt`` + ``meta.detail`` fields the TUI renderer
relies on. Tier 2 tests for #163 used a hand-built UserIntervention;
this script exercises the **real** permission resolver → intervention
handler → outbox pipeline.

Scenario: a chat user asks the LLM to fetch a URL that isn't
pre-approved. The expected flow:

  1. router_loop dispatches `web_fetch(url=...)` op.
  2. `op_runtime/web.py` calls `require_web_fetch(url, bus)`.
  3. `permissions.PermissionResolver._approve("web.fetch", "web fetch: <url>", bus)`
     → falls through Layer 1/2/3 → reaches `_prompt`.
  4. `_prompt` constructs `UserIntervention(kind="permission.generic",
     prompt="Permission request — web.fetch", detail="web fetch: <url>",
     choices=generic_yn_choices())`.
  5. `bus.request(iv)` → `InterventionHandler.announce(iv)` (production).
  6. `announce()` (post-#163) puts an OutboxMessage with both the
     concatenated text (backward-compat for CLI Panel) AND structured
     `meta.prompt` / `meta.detail` / `meta.choices` (for TUI widget).

This script runs steps 4-6 against the production code paths and dumps
the resulting OutboxMessage so a human reader can verify:

  - ``meta.prompt == "Permission request — web.fetch"`` (= bare header)
  - ``meta.detail == "web fetch: https://example.com"`` (= secondary line)
  - ``meta.choices`` has 4 entries with hotkeys (y/A/n/N)
  - ``msg.text`` still contains all three for CLI backward-compat

Run: ``python dogfood/scripts/verify_permission_prompt_structure.py``
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.chat.outbox import OutboxMessage
from reyn.chat.services.intervention_handler import InterventionHandler
from reyn.intervention_choices import generic_yn_choices
from reyn.user_intervention import UserIntervention


async def _capture_announce(iv: UserIntervention) -> OutboxMessage:
    """Run the production announce() path and capture the produced msg."""
    captured: list[OutboxMessage] = []

    async def _put(msg: OutboxMessage) -> None:
        captured.append(msg)

    handler = InterventionHandler(
        intervention_registry=None,  # not exercised by announce
        journal=None,
        event_log=None,
        put_outbox=_put,
        append_history=lambda *_a, **_k: None,
    )
    await handler.announce(iv)
    assert len(captured) == 1, f"expected 1 outbox msg, got {len(captured)}"
    return captured[0]


def _check(label: str, condition: bool, detail: str = "") -> bool:
    """Print a check line; return True on pass."""
    sym = "✓" if condition else "✗"
    line = f"  {sym} {label}"
    if detail:
        line += f"  — {detail}"
    print(line)
    return condition


async def main() -> int:
    print("=" * 72)
    print("Dogfood verify: production permission-prompt structure (issue #163)")
    print("=" * 72)
    print()

    # Construct the UserIntervention that _prompt would build for a
    # web.fetch request. Mirrors permissions.py:_prompt exactly.
    iv = UserIntervention(
        kind="permission.generic",
        prompt="Permission request — web.fetch",
        detail="web fetch: https://example.com",
        choices=generic_yn_choices(),
        run_id="dogfood-verify-run",
        skill_name="chat_router",
    )

    msg = await _capture_announce(iv)

    # ── 1. msg.kind + msg.text (backward-compat CLI Panel path) ──────────
    print("1. OutboxMessage shape (CLI backward-compat):")
    all_ok = True
    all_ok &= _check("kind == 'intervention'", msg.kind == "intervention")
    all_ok &= _check(
        "text contains the prompt header",
        "Permission request — web.fetch" in msg.text,
    )
    all_ok &= _check(
        "text contains the detail line",
        "web fetch: https://example.com" in msg.text,
    )
    all_ok &= _check(
        "text contains the choices labels (hotkeys visible)",
        "[y]es" in msg.text and "[A]lways" in msg.text,
    )

    # ── 2. meta.prompt + meta.detail (structured fields for TUI) ─────────
    print()
    print("2. meta.prompt + meta.detail (structured TUI rendering, post-#163):")
    all_ok &= _check(
        "meta.prompt == bare prompt header (no 'Question: ' prefix for permission kind)",
        msg.meta.get("prompt") == "Permission request — web.fetch",
        detail=f"got: {msg.meta.get('prompt')!r}",
    )
    all_ok &= _check(
        "meta.detail == secondary line",
        msg.meta.get("detail") == "web fetch: https://example.com",
        detail=f"got: {msg.meta.get('detail')!r}",
    )

    # ── 3. meta.choices (chip data for TUI) ──────────────────────────────
    print()
    print("3. meta.choices (chip rendering data):")
    choices = msg.meta.get("choices") or []
    all_ok &= _check(
        f"4 choices present (got {len(choices)})", len(choices) == 4,
    )
    if choices:
        ids = [c.get("id") for c in choices]
        all_ok &= _check(
            "choice IDs are yes / always / no / never",
            ids == ["yes", "always", "no", "never"],
            detail=f"got: {ids}",
        )
        hotkeys = [c.get("hotkey") for c in choices]
        all_ok &= _check(
            "hotkeys present on all 4 (y / A / n / N)",
            all(h for h in hotkeys),
            detail=f"got: {hotkeys}",
        )

    # ── 4. provenance meta (skill_name + run_id) ─────────────────────────
    print()
    print("4. provenance meta (skill_name / run_id):")
    all_ok &= _check(
        "meta.skill_name preserved",
        msg.meta.get("skill_name") == "chat_router",
    )
    all_ok &= _check(
        "meta.run_id preserved",
        msg.meta.get("run_id") == "dogfood-verify-run",
    )

    # ── 5. Dump the full payload for human inspection ────────────────────
    print()
    print("=" * 72)
    print("Full OutboxMessage payload (human inspection):")
    print("=" * 72)
    payload = {
        "kind": msg.kind,
        "text": msg.text,
        "meta": dict(msg.meta),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()

    if all_ok:
        print("RESULT: ✓ all checks passed — production permission-prompt")
        print("        structure post-#163 is intact.")
        return 0
    print("RESULT: ✗ one or more checks failed — see above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
