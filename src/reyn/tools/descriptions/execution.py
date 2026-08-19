"""Tool descriptions for the ``execution`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``execution``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``execution.NAME.text`` so every
call site is unchanged.

Covers: the ``exec`` tool (``exec.py``, renamed from ``sandboxed_exec``
#3226 Phase 3 — the op_runtime kind stays ``sandboxed_exec``, only the
tool/qualified name changed). #3226 Phase 1: the ``shell`` tool description
this module used to also cover (thin pipeline-DSL sugar over sandboxed_exec,
#2593) was removed along with the tool itself — its only production path
built ``/bin/sh -c <command>``, the sole shell-injection surface in the
codebase.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

exec_ = ToolDescription(
    tool_name="exec",
    surfaced=(
        "router (gates.router=allow) — FP-0034 exec category, always "
        "visible (#4932: no longer visibility-gated on a configured "
        "sandbox backend; isolation state is disclosed in the "
        "description text instead)"
    ),
    purpose=(
        "Execute a command in a sandboxed environment (FP-0017), with the "
        "sandbox policy (network + filesystem scope) resolved by the OS, "
        "not chosen by the LLM. #3903①: the wall-clock timeout is the one "
        "policy axis the LLM MAY extend, up to the operator's own "
        "configured ceiling."
    ),
    text=(
        "Execute a command in a sandboxed environment (FP-0017). The sandbox "
        "policy (network access + filesystem scope) is the OPERATOR's, "
        "resolved by the OS — it is not chosen here. "
        "argv: command and arguments (argv[0] is the executable). "
        "timeout: optional — extends the wall-clock timeout past its "
        "operator-configured default, up to the operator's own configured "
        "maximum; a request above that maximum is rejected, and the "
        "rejection names the actual maximum. If you need longer than that, "
        "run it in the background instead (spawn an ephemeral session, or "
        "run_pipeline with collect=\"async\") — background work runs on a "
        "separate budget from this foreground wall-clock cap, and you can "
        "stop it with cancel_task."
    ),
    ja=(
        "サンドボックス環境内でコマンドを実行する（FP-0017）。サンドボックス"
        "ポリシー（ネットワークアクセス・ファイルシステムスコープ）はオペレー"
        "ターのものとして OS が解決する（ここで選択するものではない）。"
        "argv: コマンドと引数（argv[0] が実行ファイル）。"
        "timeout: 任意 — オペレーター設定の既定タイムアウトを、オペレーター"
        "自身が設定した上限まで延長できる。上限を超える要求は拒否され、"
        "拒否時に実際の上限値が示される。それ以上必要な場合はバックグラウン"
        "ドで実行すること（一時セッションを生成する、または run_pipeline を"
        "collect=\"async\" で使う）— バックグラウンドの作業はこの前景ウォー"
        "ルクロック上限とは別の予算で動作し、cancel_task で停止できる。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "exec": exec_,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# #3962: the "timeout" entry this dict used to carry was removed along with
# the tool parameter it described — the wall-clock cap was never actually
# settable via the op on the real path (ctx.default_sandbox_policy's own
# timeout_seconds always governed), so the LLM-facing parameter and its
# description were pure advertised-but-ignored surface.
#
# #3903① (2026-08-11): "timeout" is back, with a real reader this time
# (op_runtime/sandboxed_exec.py). Deliberately no number in this text —
# lead-coder ruling: the operator's configured default/max are per-
# deployment config values, not something safe to bake into a static
# schema string (the exact "restate a count instead of reading the
# registry" drift class named #4158/#4160/#4169/proposal-0067 tonight —
# the schema would silently lie the moment an operator changed the
# config). The authoritative numbers reach the LLM at the moment it needs
# them: the rejection error (op_runtime/sandboxed_exec.py) names the
# ACTUAL configured maximum when a request exceeds it. Dynamically
# injecting the resolved numbers into this static text would need new
# schema-enrichment infra `exec`'s catalog-dispatch path doesn't have
# today (schema_enricher only reaches router_tools.build_tools()'s
# ToolSpec path, not universal_catalog's describe path) — explicitly
# deferred, not silently dropped: file an issue if/when that infra is
# actually needed.

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "exec": {
        "argv": ParamDescription(
            text="Command and arguments; argv[0] is the executable.",
            ja="コマンドと引数。argv[0] が実行ファイル。",
        ),
        "timeout": ParamDescription(
            text=(
                "Optional wall-clock timeout override in seconds, extending "
                "the operator-configured default. Capped at the operator's "
                "own configured maximum — a request above it is rejected, "
                "and the rejection names the actual maximum."
            ),
            ja=(
                "任意のウォールクロックタイムアウト（秒）— オペレーター設定"
                "の既定値を延長する。オペレーター自身が設定した上限で頭打ち"
                "になり、上限を超える要求は拒否され、拒否時に実際の上限値が"
                "示される。"
            ),
        ),
    },
}
