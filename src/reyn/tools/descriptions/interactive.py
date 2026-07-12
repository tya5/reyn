"""Tool descriptions for the ``interactive`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): ``ask_user`` (ADR-0026 M3 Wave 1),
the phase-only user-intervention tool. ``.text`` is copied verbatim from
``tools/ask_user.py``; the origin module now aliases
``_ASK_USER_DESCRIPTION`` to ``interactive.ask_user.text``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

ask_user = ToolDescription(
    tool_name="ask_user",
    surfaced="phase-only (gates.router=deny, gates.phase=allow)",
    purpose=(
        "Pause phase execution to ask the user a clarifying question, "
        "resuming with the free-text answer as a control IR result."
    ),
    text=(
        "Pause the current phase and ask the user a clarifying question. "
        "The OS suspends execution, presents the question (and optional "
        "suggestions) to the user, waits for a free-text answer, and "
        "resumes the phase with the answer available as a control IR result. "
        "question: the question to display to the user. "
        "suggestions: optional list of suggested responses. "
        "required: if true (default), an empty answer is rejected."
    ),
    ja=(
        "現在のフェーズを一時停止し、ユーザーに確認質問をする。OS が実行"
        "を中断し、質問（と任意の提案）をユーザーに提示、自由記述の回答"
        "を待ってから、その回答を control IR の結果として使えるようにフ"
        "ェーズを再開する。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "ask_user": ask_user,
}
