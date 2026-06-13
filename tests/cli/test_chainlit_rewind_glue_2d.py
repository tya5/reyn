"""Tier 1: chainlit /rewind picker glue wiring (ADR-0038 2d-2).

The branch-tree → action-spec shaping and the checkout handler are
chainlit-free and CI-tested in ``tests/test_rewind_actions_2d.py``; this
file pins only the thin ``cl.*`` glue in ``chainlit_app.app`` — that the
module imports under a real chainlit install and exposes the picker
renderer + the ``rewind_checkout`` action callback. It is
``importorskip``-guarded, so it skips in environments without the
``chainlit`` extra (= reyn CI) and runs unguarded where chainlit is
installed (tui-coder real-env verify). The interactive bare-/rewind →
tree → checkout UI flow itself is a manual Test-plan smoke.
"""
from __future__ import annotations

import inspect

import pytest

pytest.importorskip("chainlit")

from reyn.chainlit_app import app as chainlit_app  # noqa: E402


def test_render_rewind_picker_is_async_glue():
    """Tier 1: the picker renderer ships as an async function (it awaits
    ``cl.Message(...).send()``)."""
    fn = getattr(chainlit_app, "_render_rewind_picker", None)
    assert fn is not None, "app.py must define _render_rewind_picker"
    assert inspect.iscoroutinefunction(fn)


def test_rewind_checkout_callback_registered():
    """Tier 1: the ``rewind_checkout`` action callback exists as an async
    handler (= the picker buttons name=\"rewind_checkout\" have a target)."""
    fn = getattr(chainlit_app, "on_rewind_checkout", None)
    assert fn is not None, "app.py must define on_rewind_checkout"
    assert inspect.iscoroutinefunction(fn)
