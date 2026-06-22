"""Tier 2: _on_intervention degrades a malformed choice instead of dropping the
load-bearing intervention.

The choice-normalization in ``OutboxRouter._on_intervention`` documents (in its
own comment) that "missing keys fall back to safe blanks", but historically read
``c["label"]`` / ``c["id"]`` with bracket access — a KeyError on a malformed
choice. Interventions are load-bearing (the user must see + answer a permission
request); the outbox consume loop's try/except swallows a handler raise, so a
KeyError here would silently DROP the intervention, leaving the user unable to
respond. The fix aligns label/id with the documented intent + the hotkey/default
fields (all ``.get()``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_intervention_with_malformed_choice_still_mounts() -> None:
    """Tier 2: a choice missing label/id degrades (safe blanks) and the
    intervention still mounts — rather than raising KeyError + being dropped."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation")
        header = app.query_one("#header")
        router = OutboxRouter(app)
        msg = OutboxMessage(
            kind="intervention",
            text="Approve?",
            meta={
                "intervention_id": "iv-degrade-1",
                "prompt": "Approve the operation?",
                # First choice is malformed (no label AND no id) — pre-fix the
                # bracket access raised KeyError here, dropping the whole iv.
                "choices": [{"hotkey": "y"}, {"label": "No", "id": "n"}],
            },
        )
        # Direct call (no outer try/except): pre-fix this raises KeyError;
        # post-fix it degrades the malformed choice and mounts the intervention.
        router._on_intervention(msg, conv, header)
        await pilot.pause()
        assert app.query(InterventionWidget), (
            "intervention must still mount (not be dropped) when a choice is malformed"
        )
