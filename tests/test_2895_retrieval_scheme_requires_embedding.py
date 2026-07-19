"""Tier 2: #2895 fix (a) — config-load fail-loud for ``tool_use.chat:
retrieval`` selected with no working embedding configured.

``RetrievalScheme`` (``reyn.tools.schemes.retrieval``) presents a
``search_actions`` tool as its ONLY catalog entry point. Without an
embedding, ``SchemeOps.search_actions`` (``reyn.runtime.router_loop.py``)
always returns ``[]`` (index/provider unavailable — a silent degrade by
design), and retrieval's own terminal-on-empty-match rule then drops the
search tool on the very first call — stranding the LLM on ``base_tools``
only for the rest of the session, with no catalog action ever reachable
(#2895). ``reyn.config.loader._validate_retrieval_scheme_embedding`` catches
the common "never configured" case at config load instead of letting it
reach a live session.

No mocks: drives the real ``load_config`` end to end against a real
``reyn.yaml`` on disk (mirrors the pattern in
``tests/test_action_retrieval_wiring.py``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import ReynConfig, load_config


def _write_yaml(tmp_path: Path, body: str) -> None:
    (tmp_path / "reyn.yaml").write_text(body, encoding="utf-8")


def test_retrieval_scheme_without_embedding_class_fails_loud(tmp_path: Path) -> None:
    """Tier 2: #2895 fix (a), falsify-pin. ``tool_use.chat: retrieval`` +
    ``action_retrieval.embedding_class: null`` (explicit no-embedding) must
    raise a ValueError carrying the SAME enable-hint text the graceful
    schemes surface via ``list_actions`` (``universal_catalog.
    _HIDDEN_STATE_HINT``) — not a bare/opaque error, so the operator is told
    how to fix it.

    Falsify by hand: remove the
    ``_validate_retrieval_scheme_embedding(_cfg)`` call in
    ``reyn.config.loader.load_config`` → this test goes RED (load_config
    returns cleanly with the misconfiguration silently accepted).
    """
    from reyn.tools.universal_catalog import _HIDDEN_STATE_HINT

    _write_yaml(
        tmp_path,
        """
tool_use:
  chat: retrieval
action_retrieval:
  embedding_class: null
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(cwd=tmp_path)
    message = str(excinfo.value)
    assert "retrieval" in message
    assert _HIDDEN_STATE_HINT in message


def test_retrieval_scheme_with_dangling_embedding_class_fails_loud(tmp_path: Path) -> None:
    """Tier 2: an ``embedding_class`` naming no real entry in
    ``embedding.classes`` (typo) is degraded to None by
    ``_reconcile_embedding_class`` BEFORE the #2895 validation runs — so a
    dangling class is caught too, not just the explicit-null case. Reuses
    the same reconciliation ``is_search_available`` relies on at runtime
    (closed-world class membership), not a new notion of "configured"."""
    _write_yaml(
        tmp_path,
        """
tool_use:
  chat: retrieval
action_retrieval:
  embedding_class: totally-made-up-class
""",
    )
    with pytest.raises(ValueError):
        load_config(cwd=tmp_path)


def test_retrieval_scheme_with_embedding_class_loads_cleanly(tmp_path: Path) -> None:
    """Tier 2: the contrast — ``tool_use.chat: retrieval`` with a REAL
    embedding class configured loads without error (the fail-loud check is
    scoped to the no-embedding misconfiguration, not to selecting retrieval
    at all)."""
    _write_yaml(
        tmp_path,
        """
tool_use:
  chat: retrieval
action_retrieval:
  embedding_class: standard
""",
    )
    cfg: ReynConfig = load_config(cwd=tmp_path)
    assert cfg.tool_use.chat == "retrieval"
    assert cfg.action_retrieval.embedding_class == "standard"


def test_other_schemes_do_not_require_embedding(tmp_path: Path) -> None:
    """Tier 2: the validation is retrieval-specific — enumerate-all (and any
    non-retrieval scheme) never hinges its only catalog entry point on
    search, so it must NOT be rejected for having no embedding configured."""
    _write_yaml(
        tmp_path,
        """
tool_use:
  chat: enumerate-all
action_retrieval:
  embedding_class: null
""",
    )
    cfg: ReynConfig = load_config(cwd=tmp_path)
    assert cfg.tool_use.chat == "enumerate-all"
    assert cfg.action_retrieval.embedding_class is None
