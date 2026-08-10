"""Tool descriptions for the ``pipeline`` bucket (pipeline launch verbs).

Proposal 0067 P7 (#3978): unified the four pipeline-launch verbs
(``run_pipeline`` / ``run_pipeline_async`` — a REGISTERED pipeline by name —
and ``run_pipeline_inline`` / ``run_pipeline_inline_async`` — an
agent-GENERATED ad-hoc DSL string) into ONE ``run_pipeline`` tool, retired
with 0 aliases (architect ruling). ``collect`` (default ``"attached"``)
replaces the sync/async split; ``on_settle`` (P4's vocabulary) is new,
accepted for ``collect="async"`` only. ``name``/``definition`` stay as their
pre-unification param names (no rename — architect ruling: the "inline as
arguments" line in the proposal's own interface table named the AXIS, not a
param spelling). This description is authored fresh, not a relocation — the
pre-unification texts described four SEPARATE tools, and reusing one
verbatim would misdescribe the merged surface.

``pipeline_list`` (#3026) is unaffected by P7 — the read-only discovery verb
that names the REGISTERED pipelines ``run_pipeline(name=...)`` can launch.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

pipeline_list = ToolDescription(
    tool_name="pipeline_list",
    surfaced="router (gates.router=allow)",
    purpose=(
        "The discovery half of the pipeline launch surface (#3026): names the "
        "REGISTERED pipelines so `run_pipeline`'s `name` argument is "
        "answerable. Replaces the per-pipeline catalog "
        "actions, whose count scaled with the operator's pipelines."
    ),
    text=(
        "List the pipelines registered in this session, with each pipeline's "
        "name and description. Call this before `run_pipeline` when you do "
        "not already know a pipeline name: the names are chosen by the "
        "operator, so they cannot be guessed. An empty list means no "
        "pipelines are registered — say so rather than guessing a name."
    ),
    ja=(
        "このセッションに登録済みのパイプラインを名前・説明つきで列挙する。"
        "run_pipeline の name は運用者が決めた名前なので推測できない。"
        "名前を知らない場合はまずこれを呼ぶ。#3026 でパイプラインごとの"
        "アクション（= payload が運用者のパイプライン数に比例して増える原因）を畳んだ"
        "代わりに置かれた、定数個の discovery verb。"
    ),
)

run_pipeline = ToolDescription(
    tool_name="run_pipeline",
    surfaced="router (gates.router=allow)",
    purpose=(
        "Launch a pipeline — REGISTERED (by name) or ad-hoc (an inline DSL "
        "definition you generate) — either attached (block for the result) "
        "or async (return immediately, result arrives later as a [pipeline] "
        "message)."
    ),
    text=(
        "Run a pipeline: pass exactly one of 'name' (a REGISTERED pipeline) or "
        "'definition' (an ad-hoc pipeline you define inline as a DSL string, "
        "Appendix B grammar — statically validated: parse, schema refs, tool "
        "names, no nested pipeline launch, agent steps run as you — BEFORE "
        "anything is spawned). 'collect' picks how you get the result: "
        "'attached' (default) blocks until the pipeline finishes and returns "
        "its final output; 'async' returns immediately with "
        "{status: started, run_id} and the result arrives later as a "
        "[pipeline] message. 'input' seeds the pipeline's initial named "
        "context (ctx.*) for its first step. 'on_settle' ('deliver' default | "
        "a pipeline name | 'drop') controls what happens to the result — "
        "only meaningful for collect='async' (ignored for 'attached', which "
        "always delivers inline). Fails clearly if 'name' is not registered, "
        "'definition' is invalid, or a step fails."
    ),
    ja=(
        "パイプラインを実行する: 'name'（登録済みパイプライン）か "
        "'definition'（DSL文字列でインラインに定義するアドホックなパイプ"
        "ライン、Appendix B 文法 — 何かが起動される前にパース・スキーマ"
        "参照・ツール名・パイプラインのネスト起動禁止・agent ステップは"
        "呼び出し元として実行、を静的検証）のどちらか一方を渡す。"
        "'collect' で結果の受け取り方を選ぶ: 'attached'（既定）はパイプ"
        "ライン完了まで待ち最終出力を返す。'async' は即座に "
        "{status: started, run_id} を返し、結果は後で [pipeline] メッセ"
        "ージとして届く。'input' はパイプライン最初のステップの初期名前"
        "付きコンテキスト（ctx.*）。'on_settle'（既定 'deliver' | パイプ"
        "ライン名 | 'drop'）は結果の扱いを制御する — collect='async' の"
        "ときのみ意味を持つ（'attached' では常にインライン配送のため無視"
        "される）。'name' が未登録、'definition' が不正、いずれかのステ"
        "ップが失敗した場合は明確に失敗する。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "run_pipeline": run_pipeline,
    "pipeline_list": pipeline_list,
}


# ── Phase 4: per-parameter descriptions ───────────────────────────────────

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "run_pipeline": {
        "name": ParamDescription(
            text="The registered pipeline's name. Exactly one of name/definition.",
            ja="登録済みパイプラインの名前。name/definition のどちらか一方。",
        ),
        "definition": ParamDescription(
            text=(
                "The pipeline as a DSL string (Appendix B grammar): one or "
                "more '---'-separated YAML documents — exactly one 'pipeline:' "
                "document plus any 'schema:' documents its steps reference. "
                "Generated at call time; parsed + statically validated before "
                "anything runs. Exactly one of name/definition."
            ),
            ja=(
                "DSL文字列としてのパイプライン（Appendix B 文法）。1つ以上の "
                "'---' 区切り YAML ドキュメント — 'pipeline:' ドキュメントが"
                "1つ必須、ステップが参照する 'schema:' ドキュメントは任意。"
                "呼び出し時に生成され、実行前にパースと静的検証を受ける。"
                "name/definition のどちらか一方。"
            ),
        ),
        "input": ParamDescription(
            text=(
                "Initial named context (ctx.*) for the pipeline's first "
                "step. Omit for a pipeline that needs no seed input."
            ),
            ja=(
                "パイプライン最初のステップ向けの初期名前付きコンテキスト"
                "（ctx.*）。シード入力が不要なパイプラインなら省略可。"
            ),
        ),
        "collect": ParamDescription(
            text=(
                "'attached' (default): block until the pipeline finishes, "
                "return its final output inline. 'async': return immediately "
                "with {status: started, run_id}; the result arrives later as "
                "a [pipeline] message."
            ),
            ja=(
                "'attached'（既定）: パイプライン完了まで待ち最終出力を"
                "インラインで返す。'async': 即座に {status: started, run_id} "
                "を返し、結果は後で [pipeline] メッセージとして届く。"
            ),
        ),
        "on_settle": ParamDescription(
            text=(
                "'deliver' (default) | a pipeline name | 'drop' — what "
                "happens to the result. Only meaningful for collect='async' "
                "(ignored for 'attached', which always delivers inline)."
            ),
            ja=(
                "'deliver'（既定）| パイプライン名 | 'drop' — 結果の扱い。"
                "collect='async' のときのみ意味を持つ（'attached' では常に"
                "インライン配送のため無視される）。"
            ),
        ),
    },
}
