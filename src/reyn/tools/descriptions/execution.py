"""Tool descriptions for the ``execution`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``execution``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``execution.NAME.text`` so every
call site is unchanged.

Covers: sandboxed_exec (``sandboxed_exec.py``), shell (``shell.py`` —
pipeline DSL sugar over sandboxed_exec, #2593).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

sandboxed_exec = ToolDescription(
    tool_name="sandboxed_exec",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — FP-0034 "
        "exec category, visibility-gated on a configured sandbox backend"
    ),
    purpose=(
        "Execute a command in a sandboxed environment (FP-0017), with the "
        "sandbox policy (network + filesystem scope) resolved by the OS, "
        "not chosen by the LLM."
    ),
    text=(
        "Execute a command in a sandboxed environment (FP-0017). The sandbox "
        "policy (network access + filesystem scope) is the OPERATOR's, resolved "
        "by the OS — it is not chosen here. "
        "argv: command and arguments (argv[0] is the executable). "
        "timeout_seconds: wall-clock time limit in seconds (default 60)."
    ),
    ja=(
        "サンドボックス環境内でコマンドを実行する（FP-0017）。サンドボックス"
        "ポリシー（ネットワークアクセス・ファイルシステムスコープ）は"
        "オペレーターのものとして OS が解決する（ここで選択するものでは"
        "ない）。argv: コマンドと引数、timeout_seconds: 秒単位のタイムアウト"
        "（デフォルト60）。"
    ),
)

shell = ToolDescription(
    tool_name="shell",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — pipeline "
        "DSL ``shell`` step sugar over sandboxed_exec (#2593)"
    ),
    purpose=(
        "Run a shell command as pipeline-DSL sugar: STDIN carries the "
        "previous step's pipe-data, STDOUT becomes this step's output, "
        "same sandbox confinement as sandboxed_exec."
    ),
    text=(
        "Run a shell command (via sandboxed_exec) whose STDIN receives the "
        "previous pipeline step's pipe-data JSON-encoded, and whose STDOUT "
        "becomes this step's output. command: the shell command line "
        "(argv[0]='/bin/sh', argv[1]='-c'). timeout: wall-clock time limit in "
        "seconds (default 60). The sandbox policy (network access + filesystem "
        "scope) is the OPERATOR's, resolved by the OS — it is not chosen here."
    ),
    ja=(
        "シェルコマンドを実行する（sandboxed_exec 経由）。STDIN には前段の"
        "パイプラインステップの pipe-data が JSON エンコードされて渡り、"
        "STDOUT がこのステップの出力になる。command: シェルコマンドライン"
        "（argv[0]='/bin/sh', argv[1]='-c'）。timeout: 秒単位のタイムアウト"
        "（デフォルト60）。サンドボックスポリシーはオペレーターのものとして"
        "OS が解決する。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "sandboxed_exec": sandboxed_exec,
    "shell": shell,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

_timeout_seconds_desc = ParamDescription(
    text="Wall-clock time limit in seconds (default 60).",
    ja="実時間タイムアウト秒数（デフォルト 60）。",
)

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "sandboxed_exec": {
        "argv": ParamDescription(
            text="Command and arguments; argv[0] is the executable.",
            ja="コマンドと引数。argv[0] が実行ファイル。",
        ),
        "timeout_seconds": _timeout_seconds_desc,
    },
    "shell": {
        "command": ParamDescription(
            text="Shell command line, run as `/bin/sh -c <command>`.",
            ja="`/bin/sh -c <command>` として実行されるシェルコマンド行。",
        ),
        "stdin_pipe": ParamDescription(
            text="The previous pipeline step's pipe-data (JSON-encoded onto stdin).",
            ja="直前のパイプラインステップのパイプデータ（JSON エンコードして stdin に渡す）。",
        ),
        "timeout": _timeout_seconds_desc,
    },
}
