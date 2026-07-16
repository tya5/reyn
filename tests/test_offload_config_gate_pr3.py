"""Tier 1/2: ``offload:`` config opt-in — 3-gate enable (tool-result-schema-redesign §5, PR-3;
owner-confirmed opt-out -> opt-in default flip).

Tier 1 (contract): the ``offload:`` reyn.yaml section parses ``enabled`` (default
False, opt-in) via ``load_config`` — a real config file round-trip with the non-default
value, per feedback_roundtrip_test_nondefault_value (config parsing alone
doesn't prove the flag reaches the actual gates — the Tier 2 tests below do
that, through each seam's PUBLIC surface, no private-state asserts).

Tier 2 (OS invariant): with a real ``ContextBudgetAdvisor`` / real
``build_offload_body`` call constructed with ``offload_config.enabled=True``,
all three size gates the design doc names are active: the text token cap
(``cap_tool_result``), the structured inline gate (``build_offload_body``), and
the media follow-up budget (``media_followup_budget``). Per the design doc's
explicit ban, none of these tests achieve the effect by forcing
``per_turn_cap_tokens()`` to 0 (that would zero ``media_followup_budget`` too,
for the wrong reason) — each gate is exercised through its own real seam, and
``per_turn_cap_tokens()`` is asserted to stay untouched.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config import CompactionConfig, OffloadConfig, _build_offload_config
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _advisor(*, offload_config: OffloadConfig, media_store=None) -> ContextBudgetAdvisor:
    return ContextBudgetAdvisor(
        compaction=CompactionConfig(),
        compaction_controller=None,  # → effective_trigger straight from the model window
        media_store=media_store,
        model_fn=lambda: "openai/gpt-4o",
        events=None,
        history_fn=lambda: [],
        offload_config=offload_config,
    )


# ── Tier 1: config parsing round-trip ───────────────────────────────────────


def test_offload_config_defaults_to_disabled():
    """Tier 1: no ``offload:`` section in reyn.yaml -> OffloadConfig(enabled=False)
    (opt-in: offload is off unless explicitly enabled)."""
    assert _build_offload_config(None) == OffloadConfig(enabled=False)
    assert _build_offload_config({}) == OffloadConfig(enabled=False)


def test_load_config_reads_offload_enabled_true(tmp_path):
    """Tier 1: a real reyn.yaml with ``offload.enabled: true`` reaches
    ``ReynConfig.offload.enabled`` via ``load_config`` (non-default-value
    round-trip, feedback_roundtrip_test_nondefault_value)."""
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\noffload:\n  enabled: true\n",
    )
    config = load_config(cwd=tmp_path)
    assert config.offload.enabled is True


# ── Tier 2: gate 1 — text token cap (ContextBudgetAdvisor.cap_tool_result) ──


def test_offload_disabled_default_skips_text_cap(tmp_path):
    """Tier 2: with a real MediaStore wired (so the enabled=True path WOULD
    cap), ``offload.enabled=False`` (the default) still returns an oversized
    tool-result unchanged — the text-cap gate never fires.

    Falsification: pre-flip (offload on by default) this would be capped
    down to a bounded plain-text preview, and the ``== oversized`` assertion
    would fail.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    advisor = _advisor(offload_config=OffloadConfig(enabled=False), media_store=store)
    oversized = "x" * 200_000  # far beyond any realistic per-turn cap
    assert advisor.cap_tool_result(oversized) == oversized


def test_offload_enabled_opted_in_caps_oversized_text(tmp_path):
    """Tier 2: the same oversized text, same MediaStore, but
    ``offload.enabled=True`` (explicit opt-in) — the text-cap gate DOES fire,
    proving the prior test's unchanged result comes from the flag, not a
    broken capper."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    advisor = _advisor(offload_config=OffloadConfig(enabled=True), media_store=store)
    oversized = "x" * 200_000
    capped = advisor.cap_tool_result(oversized)
    assert capped != oversized
    assert len(capped) < len(oversized)


# ── Tier 2: gate 2 — structured inline gate (build_offload_body) ───────────


def test_offload_disabled_default_keeps_oversized_structured_inline():
    """Tier 2: ``build_offload_body(enabled=False)`` (the default) never
    offloads a structured attachment to a ``structured_ref`` regardless of
    size — the ``STRUCTURED_INLINE_MAX_CHARS`` gate is the one this flag
    gates."""
    from reyn.core.offload.seam import STRUCTURED_INLINE_MAX_CHARS, build_offload_body

    big = {"items": list(range(STRUCTURED_INLINE_MAX_CHARS))}  # serializes far over the gate

    def _save_fn(serialized, tool=""):
        raise AssertionError("save_fn must not be called when offload is disabled")

    canonical = {"text": "", "attachments": [{"kind": "structured", "data": big}], "meta": {}}
    frontmatter, _text, _media, _ct = build_offload_body(canonical, save_fn=_save_fn, enabled=False)
    assert frontmatter["structured"] == big
    assert "structured_ref" not in frontmatter


def test_offload_enabled_opted_in_offloads_oversized_structured():
    """Tier 2: the same oversized structured attachment IS offloaded
    to a ref when ``enabled=True`` (explicit opt-in) — confirms the prior
    test's inline result is caused by the flag, not by a broken gate."""
    from reyn.core.offload.seam import STRUCTURED_INLINE_MAX_CHARS, build_offload_body

    big = {"items": list(range(STRUCTURED_INLINE_MAX_CHARS))}
    saved = {}

    def _save_fn(serialized, tool=""):
        saved["tool"] = tool
        return {"path": "/tmp/fake-structured-ref.json"}

    canonical = {"text": "", "attachments": [{"kind": "structured", "data": big}], "meta": {}}
    frontmatter, _text, _media, _ct = build_offload_body(canonical, save_fn=_save_fn, enabled=True)
    assert frontmatter["structured"] == "offloaded"
    assert frontmatter["structured_ref"] == "/tmp/fake-structured-ref.json"
    assert saved["tool"] == "structured"


# ── Tier 2: gate 3 — media follow-up budget ─────────────────────────────────


def test_offload_disabled_default_media_followup_budget_is_unbounded():
    """Tier 2: with ``offload.enabled=False`` (the default), ``media_followup_budget``
    returns ``None`` (unbounded) — the design doc's explicit reasoning for
    including this gate: ``per_turn_cap_tokens()`` stays untouched (never
    forced to 0, which would also be read by this same computation); the flag
    intercepts the call at its own seam instead.

    Falsification: pre-flip (offload on by default) this would return
    ``max(0, per_turn_cap_tokens() - text_tokens)`` (a finite int), and the
    ``is None`` assertion would fail.
    """
    advisor = _advisor(offload_config=OffloadConfig(enabled=False))
    assert advisor.media_followup_budget("some tool text") is None
    # per_turn_cap_tokens itself is untouched by the flag (design ban on the
    # zero-it-out shortcut) — it still computes a real positive value.
    assert advisor.per_turn_cap_tokens() > 0


def test_offload_enabled_opted_in_media_followup_budget_is_bounded():
    """Tier 2: with ``offload.enabled=True`` (explicit opt-in), the media
    follow-up budget IS a finite int derived from the per-turn cap minus the
    text's token estimate — confirms the prior test's ``None`` is caused by
    the flag, not a pre-existing unbounded default."""
    advisor = _advisor(offload_config=OffloadConfig(enabled=True))
    budget = advisor.media_followup_budget("some tool text")
    assert isinstance(budget, int)
    assert budget >= 0
