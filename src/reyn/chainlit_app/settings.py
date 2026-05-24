"""Per-session chainlit settings panel for runtime knobs.

``cl.ChatSettings`` lets the operator flip per-session knobs from a
gear icon next to the input box without restarting ``reyn chainlit``.
Today we expose one: the LLM ``output_language`` directive that
otherwise lives in ``reyn.yaml`` and is fixed at process startup.

Pure helper, no chainlit import — unit tests run without the
``[chainlit]`` extra installed.
"""
from __future__ import annotations

# Settings widget id used as the dict key in ``cl.on_settings_update``.
LANGUAGE_SETTING_ID = "output_language"

# ``_AUTO_VALUE`` represents "let the LLM pick based on the user's
# input" (= ``session.output_language = None`` reyn-side). Stored as a
# real string because chainlit's Select widget requires non-null values
# in its ``items`` map — we round-trip it back to ``None`` on apply.
_AUTO_VALUE = "auto"

# label → value. Chainlit's Select widget shows the label in the
# dropdown; ``value`` lands in the ``on_settings_update`` payload.
# Curated short list — adding a 5th option is just a new dict entry.
LANGUAGE_ITEMS: dict[str, str] = {
    "Auto (LLM decides)": _AUTO_VALUE,
    "日本語": "ja",
    "English": "en",
    "中文": "zh",
    "한국어": "ko",
}


def language_to_value(lang: str | None) -> str:
    """Map ``ChatSession.output_language`` → widget select value.

    None / empty → ``"auto"`` (= the widget always renders a concrete
    selection, never blank). Known codes pass through; unknown codes
    also pass through so a user's custom yaml setting survives the
    round-trip even if not in ``LANGUAGE_ITEMS`` (= chainlit shows the
    raw code as the active value).
    """
    if lang is None or not lang.strip():
        return _AUTO_VALUE
    return lang.strip()


def value_to_language(value: str | None) -> str | None:
    """Map widget select value → ``ChatSession.output_language``.

    ``"auto"`` / None / empty → ``None`` (= let the LLM pick). Any
    other value passes through verbatim so a future widget item
    addition reaches reyn without an additional dispatch table.
    """
    if value is None:
        return None
    v = value.strip()
    if not v or v == _AUTO_VALUE:
        return None
    return v


def language_label_for(value: str) -> str:
    """Reverse lookup: value → human label (= for confirmation messages).

    Falls back to the raw value when not in the curated dict (= a
    custom yaml-set code still gets a readable acknowledgement instead
    of an empty cell).
    """
    for label, v in LANGUAGE_ITEMS.items():
        if v == value:
            return label
    return value


# ── model select ──────────────────────────────────────────────────────────


MODEL_SETTING_ID = "model"


def list_model_names(resolver: object) -> list[str]:
    """Return sorted tier names ("standard" / "claude-sonnet" / ...) the
    operator can switch between via the chainlit settings panel.

    Source of truth: ``ModelResolver._resolved`` (= built-ins +
    operator-declared from ``reyn.yaml::models``). Accessed via
    ``getattr`` so this helper stays decoupled from the resolver class
    — passing in any object with ``_resolved: dict`` works, including
    test fakes. Returns ``[]`` when the attribute is missing so the
    chainlit-side fallback can render an empty / hidden Select instead
    of crashing.
    """
    resolved = getattr(resolver, "_resolved", None)
    if not isinstance(resolved, dict):
        return []
    return sorted(resolved.keys())


def value_to_model(value: str | None, *, default: str) -> str:
    """Map widget select value → ``ChatSession.model`` tier name.

    Empty / None → ``default`` (= preserves current model rather than
    silently flipping to an arbitrary fallback). Any other value
    passes through verbatim so an operator-declared tier in reyn.yaml
    reaches reyn unchanged.
    """
    if value is None:
        return default
    v = value.strip()
    if not v:
        return default
    return v


__all__ = [
    "LANGUAGE_ITEMS",
    "LANGUAGE_SETTING_ID",
    "MODEL_SETTING_ID",
    "language_label_for",
    "language_to_value",
    "list_model_names",
    "value_to_language",
    "value_to_model",
]
