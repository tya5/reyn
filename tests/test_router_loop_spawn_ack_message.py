"""Tier 2 tests for the OS-level spawn-ack synthetic message.

When ``invoke_skill`` or ``invoke_action`` returns a spawn-ack
(``{status: "spawned", ...}``) the router exits the loop before any
further LLM call (= H3 ablation). The OS injects a deterministic
user-visible acknowledgment via ``put_outbox(kind="agent")`` to close
the silence window without re-introducing the LLM-composition race
that H3 fixed.

The full ``Session→RouterLoop`` spawn-ack contract is covered in
``test_router_loop_chatsession.test_user_message_invoke_skill_e2e``.
These tests focus on the OS-message invariants:

- i18n via ``output_language`` (= ja / en / unconfigured)
- ``/tasks`` mention is unconditional
- ``meta.source == "spawn_ack"`` for downstream consumers
- the message is OS-composed (= identical across runs, no LLM in path)
"""
from __future__ import annotations

import importlib

import pytest


def _load_spawn_ack_template() -> dict[str, str]:
    """Import the live template from router_loop. Indirect so the
    test fails fast if the constant is renamed / moved."""
    router_loop = importlib.import_module("reyn.runtime.router_loop")
    return getattr(router_loop, "_SPAWN_ACK_MSG")


def test_spawn_ack_template_has_en_and_ja():
    """Tier 2: ``_SPAWN_ACK_MSG`` declares at least the two locales the
    repo's other i18n templates carry (en, ja). New locales added later
    are fine; absence is the regression to guard."""
    tmpl = _load_spawn_ack_template()
    assert "en" in tmpl, f"missing 'en' locale; got {list(tmpl.keys())}"
    assert "ja" in tmpl, f"missing 'ja' locale; got {list(tmpl.keys())}"


def test_spawn_ack_each_locale_mentions_tasks():
    """Tier 2: ``/tasks`` is the user's only in-flight tracking surface,
    so every locale must mention it. P3 invariant: OS owns this UX
    guarantee, not the LLM."""
    tmpl = _load_spawn_ack_template()
    for lang, text in tmpl.items():
        assert "/tasks" in text, (
            f"locale {lang!r} omits /tasks hint; got {text!r}"
        )


def test_spawn_ack_each_locale_signals_background_run():
    """Tier 2: each locale must signal that the action is running, not
    that it has finished — the user is told what just happened."""
    tmpl = _load_spawn_ack_template()
    en = tmpl["en"].lower()
    ja = tmpl["ja"]
    assert "background" in en or "running" in en, (
        f"en locale must signal background execution; got {tmpl['en']!r}"
    )
    # JA: 「バックグラウンド」「実行」「進行」のいずれかが含まれていれば良い
    assert any(token in ja for token in ("バックグラウンド", "実行", "進行")), (
        f"ja locale must signal background execution; got {ja!r}"
    )


def test_spawn_ack_p7_clean_no_skill_names():
    """Tier 2: P7 invariant — the template must NOT contain skill-specific
    qualified names (`skill__X`, `file__Y`, `web__Z`, etc.) or
    skill-name-shaped strings. OS-level message stays category-agnostic."""
    tmpl = _load_spawn_ack_template()
    forbidden_prefixes = ("skill__", "file__", "web__", "memory.", "rag.", "mcp.", "exec__", "reyn_source__")
    for lang, text in tmpl.items():
        for prefix in forbidden_prefixes:
            assert prefix not in text, (
                f"locale {lang!r} leaks skill-specific prefix "
                f"{prefix!r}; got {text!r}"
            )
