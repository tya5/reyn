"""Tool descriptions for the ``interactive`` category.

Phase 3 of the tool-description package refactor relocated ``ask_user``
(ADR-0026 M3 Wave 1), the user-intervention tool, out of ``tools/ask_user.py``;
the origin module aliases ``_ASK_USER_DESCRIPTION`` to
``interactive.ask_user.text``. #2696 rewrote the text off the deleted
phase/control-IR vocabulary onto the surface that actually invokes it (a
pipeline ``tool: ask_user`` step).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

ask_user = ToolDescription(
    tool_name="ask_user",
    surfaced="not surfaced (gates.router=deny)",
    purpose=(
        "Pause the run to ask the user a clarifying question, "
        "resuming with the free-text answer as the tool result."
    ),
    text=(
        "Pause the current run and ask the user a clarifying question. "
        "The OS suspends execution, presents the question (and optional "
        "suggestions) to the user, waits for a free-text answer, and "
        "resumes with the answer returned as this tool's result. "
        "question: the question to display to the user. "
        "suggestions: optional list of suggested responses. "
        "required: if true (default), an empty answer is rejected."
    ),
    ja=(
        "実行を一時停止し、ユーザーに確認質問をする。OS が実行を中断し、"
        "質問（と任意の提案）をユーザーに提示、自由記述の回答を待ってから、"
        "その回答をこのツールの結果として返して再開する。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "ask_user": ask_user,
}
