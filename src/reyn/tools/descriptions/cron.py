"""Tool descriptions for the ``cron`` category.

Phase 3 of the tool-description package refactor (byte-identical relocation
— no LLM-facing text change): the 5 cron verbs (register / unregister /
list / enable / disable), FP-0041 #489 PR-B2's message-based cron shape.
Each ``.text`` value is copied verbatim from ``tools/cron.py``; the origin
module now aliases its ``_CRON_*_DESCRIPTION`` module constants to
``cron.NAME.text`` so every call site is unchanged.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

cron_register = ToolDescription(
    tool_name="cron_register",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose=(
        "Schedule a recurring message-to-agent job so periodic checks / "
        "reminders / summaries run without a live turn keeping them alive."
    ),
    text=(
        "Schedule a recurring message to a Reyn agent. The cron scheduler "
        "delivers the message to the target agent's inbox at each cron "
        "fire — the agent processes it as a normal attributed turn from "
        "a scheduled trigger. Idempotent on `name` (= replaces existing). "
        "Use for periodic checks, reminders, automated summaries."
    ),
    ja=(
        "Reyn エージェントへの定期メッセージをスケジュールする。cron 発火"
        "ごとに対象エージェントの受信箱にメッセージが配送され、通常のター"
        "ンとして処理される。`name` について冪等（既存を置き換える）。"
        "定期チェックやリマインダー、自動サマリーに使う。"
    ),
)

cron_unregister = ToolDescription(
    tool_name="cron_unregister",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose="Remove a previously-registered cron job so it stops firing.",
    text=(
        "Remove a previously-registered cron job by name. The schedule "
        "stops firing immediately. No-op if the job doesn't exist."
    ),
    ja=(
        "名前を指定して既存の cron ジョブを削除する。スケジュールは即座"
        "に停止する。ジョブが存在しない場合は何もしない。"
    ),
)

cron_list = ToolDescription(
    tool_name="cron_list",
    surfaced="router-only (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Enumerate current cron jobs (legacy reyn.yaml + dynamic "
        ".reyn/config/cron.yaml, unioned) so the agent can inspect schedule "
        "state before mutating it."
    ),
    text=(
        "List all currently-registered cron jobs (= both reyn.yaml legacy "
        "and .reyn/config/cron.yaml dynamic entries, unioned). Returns job name, "
        "target, message/action, schedule, enabled state, and next-run time."
    ),
    ja=(
        "現在登録されている cron ジョブを全て一覧する（reyn.yaml のレガ"
        "シー分と .reyn/config/cron.yaml の動的分を統合）。ジョブ名・対象・"
        "メッセージ/アクション・スケジュール・有効状態・次回実行時刻を返す。"
    ),
)

cron_enable = ToolDescription(
    tool_name="cron_enable",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose="Resume firing a previously-disabled cron job.",
    text=(
        "Enable a previously-disabled cron job. The scheduler resumes "
        "firing it on its schedule. No-op if already enabled."
    ),
    ja=(
        "無効化されていた cron ジョブを有効化する。スケジューラは元の"
        "スケジュールで発火を再開する。既に有効な場合は何もしない。"
    ),
)

cron_disable = ToolDescription(
    tool_name="cron_disable",
    surfaced="router-only (gates.router=allow, gates.phase=deny)",
    purpose="Pause a cron job without deleting it, for later re-enable.",
    text=(
        "Disable a cron job without removing it. The schedule stops firing "
        "until re-enabled via `cron__enable`. Use to pause a job temporarily."
    ),
    ja=(
        "cron ジョブを削除せずに無効化する。`cron__enable` で再度有効化"
        "するまでスケジュールは停止する。一時的にジョブを止めたい場合に"
        "使う。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "cron_register": cron_register,
    "cron_unregister": cron_unregister,
    "cron_list": cron_list,
    "cron_enable": cron_enable,
    "cron_disable": cron_disable,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# ``name`` is shared across register/unregister/enable/disable (the origin
# module builds ``_CRON_NAME_PARAM`` once and reuses it) — kept as one entry
# reused below rather than duplicated per tool, matching that structure.

_name_param = ParamDescription(
    text=(
        "Unique job identifier within the project (e.g. "
        "'morning_news', 'weekly_report'). Reused across "
        "register/unregister/enable/disable."
    ),
    ja=(
        "プロジェクト内で一意なジョブ識別子（例 'morning_news', "
        "'weekly_report'）。register/unregister/enable/disable で共通に使う。"
    ),
)

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "cron_register": {
        "name": _name_param,
        "to": ParamDescription(
            text=(
                "Target Reyn agent name. The scheduled message is "
                "delivered to this agent's inbox; the agent must "
                "exist in the project."
            ),
            ja=(
                "送信先の Reyn エージェント名。スケジュールされたメッセージは"
                "このエージェントの inbox に届く。プロジェクト内に実在する"
                "必要がある。"
            ),
        ),
        "message": ParamDescription(
            text=(
                "Free-form text dispatched to the agent. Treated as a "
                "user-turn-shaped message with sender='cron:<name>'."
            ),
            ja=(
                "エージェントに送る自由記述テキスト。sender='cron:<name>' の"
                "ユーザーターン形式のメッセージとして扱われる。"
            ),
        ),
        "schedule": ParamDescription(
            text=(
                "5-field cron expression (e.g. '0 9 * * *' = daily 9am, "
                "'0 */6 * * *' = every 6 hours, '0 9 * * MON' = Mondays "
                "9am)."
            ),
            ja=(
                "5フィールドの cron 式（例 '0 9 * * *' = 毎日9時、"
                "'0 */6 * * *' = 6時間毎、'0 9 * * MON' = 毎週月曜9時）。"
            ),
        ),
        "enabled": ParamDescription(
            text=(
                "Whether the schedule fires immediately. Defaults to "
                "true. Set false to register a paused job and enable "
                "later via cron__enable."
            ),
            ja=(
                "スケジュールを即座に有効化するか。デフォルト true。false に"
                "すると一時停止状態で登録し、後で cron__enable で有効化できる。"
            ),
        ),
    },
    "cron_unregister": {"name": _name_param},
    "cron_enable": {"name": _name_param},
    "cron_disable": {"name": _name_param},
}
