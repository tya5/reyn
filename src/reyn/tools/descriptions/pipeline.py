"""Tool descriptions for the ``pipeline`` bucket (pipeline launch verbs).

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the four pipeline-launch verbs
from ``tools/pipeline_verbs.py`` — ``run_pipeline`` / ``run_pipeline_async``
(a REGISTERED pipeline by name) and ``run_pipeline_inline`` /
``run_pipeline_inline_async`` (an agent-GENERATED ad-hoc DSL string,
statically validated before spawn). Each ``.text`` value is copied verbatim
from its origin constant; the origin module now aliases its
``_RUN_PIPELINE*_DESCRIPTION`` constants to ``pipeline.NAME.text``.

Note: all four carry ``ToolDefinition.category="io"`` — this module groups
them by feature-area (pipeline launch), matching the ``mcp`` / ``io``
precedent set in Phase 2 (module grouping is conceptual, not a literal
mirror of the ``category`` field).

#3026 adds a fifth, ``pipeline_list`` — the read-only discovery verb
(``category="discovery"``, grouped here by feature-area like the rest). Its
text is NOT a relocation (the tool is new): it replaced the per-pipeline
``pipeline__<name>`` catalog actions, which were the only surface naming a
REGISTERED pipeline but put one action per pipeline into the LLM's
``tools=``. A required ``name`` argument the model has no stated way to
enumerate is the same reachability gap #2971 closed for skills.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

pipeline_list = ToolDescription(
    tool_name="pipeline_list",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "The discovery half of the pipeline launch surface (#3026): names the "
        "REGISTERED pipelines so `run_pipeline`'s `name` argument is "
        "answerable. Replaces the per-pipeline `pipeline__<name>` catalog "
        "actions, whose count scaled with the operator's pipelines."
    ),
    text=(
        "List the pipelines registered in this session, with each pipeline's "
        "name and description. Call this before `pipeline__run` when you do "
        "not already know a pipeline name: the names are chosen by the "
        "operator, so they cannot be guessed. An empty list means no "
        "pipelines are registered — say so rather than guessing a name."
    ),
    ja=(
        "このセッションに登録済みのパイプラインを名前・説明つきで列挙する。"
        "pipeline__run の name は運用者が決めた名前なので推測できない。"
        "名前を知らない場合はまずこれを呼ぶ。#3026 で pipeline__<name>（= "
        "payload が運用者のパイプライン数に比例して増える原因）を畳んだ"
        "代わりに置かれた、定数個の discovery verb。"
    ),
)

run_pipeline = ToolDescription(
    tool_name="run_pipeline",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Launch a REGISTERED pipeline by name and block for its final "
        "output — the sync entry point for a pre-built multi-step "
        "workflow."
    ),
    text=(
        "Run a REGISTERED pipeline by name to completion and return its final "
        "output. Blocks until the pipeline finishes (sync). 'input' seeds the "
        "pipeline's initial named context (ctx.*) for its first step. Fails "
        "clearly if 'name' is not a registered pipeline, or if any step fails."
    ),
    ja=(
        "登録済みパイプラインを名前で実行し、完了まで待って最終出力を返"
        "す（同期）。'input' はパイプライン最初のステップの初期名前付き"
        "コンテキスト（ctx.*）を与える。'name' が未登録、またはいずれか"
        "のステップが失敗した場合は明確に失敗する。"
    ),
)

run_pipeline_async = ToolDescription(
    tool_name="run_pipeline_async",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Launch a REGISTERED pipeline in the background and return "
        "immediately, for a long-running workflow whose result arrives "
        "later as a [pipeline] message."
    ),
    text=(
        "Launch a REGISTERED pipeline by name in the background and return "
        "immediately with {status: started, run_id}. The pipeline runs in a "
        "dedicated crash-recoverable driver session; its final result arrives "
        "later as a [pipeline] message on your conversation. 'input' seeds the "
        "pipeline's initial named context (ctx.*) for its first step."
    ),
    ja=(
        "登録済みパイプラインを名前でバックグラウンド起動し、即座に "
        "{status: started, run_id} を返す。パイプラインは専用のクラッシ"
        "ュ復旧可能なドライバーセッションで実行され、最終結果は後で "
        "[pipeline] メッセージとして会話に届く。"
    ),
)

run_pipeline_inline = ToolDescription(
    tool_name="run_pipeline_inline",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Run an ad-hoc, agent-GENERATED DSL-string pipeline to completion, "
        "statically validated (parse / schema refs / tool names / no "
        "nested launch / identity==invoker) before anything spawns."
    ),
    text=(
        "Run an ad-hoc pipeline you DEFINE inline (no pre-registration) to "
        "completion and return its final output. Blocks until it finishes (sync). "
        "'definition' is a pipeline DSL string (Appendix B); 'input' seeds the "
        "first step's ctx.*. The definition is statically validated (parse, schema "
        "refs, tool names, no nested pipeline launch, agent steps run as you) "
        "BEFORE anything is spawned — a bad definition fails clearly and runs "
        "nothing."
    ),
    ja=(
        "事前登録なしでインラインに定義したアドホックなパイプラインを完"
        "了まで実行し、最終出力を返す（同期）。'definition' はパイプラ"
        "イン DSL 文字列。何かが起動される前に静的検証（パース、スキー"
        "マ参照、ツール名、パイプラインのネスト起動禁止、agent ステップ"
        "は呼び出し元として実行）が行われる — 不正な定義は明確に失敗し"
        "何も実行しない。"
    ),
)

run_pipeline_inline_async = ToolDescription(
    tool_name="run_pipeline_inline_async",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Launch an ad-hoc, agent-GENERATED DSL-string pipeline in the "
        "background, statically validated before spawn, result arriving "
        "later as a [pipeline] message."
    ),
    text=(
        "Launch an ad-hoc pipeline you DEFINE inline in the background and return "
        "immediately with {status: started, run_id}. It runs in a dedicated "
        "crash-recoverable driver session; its final result arrives later as a "
        "[pipeline] message on your conversation. 'definition' is a pipeline DSL "
        "string (Appendix B), statically validated before spawn; 'input' seeds the "
        "first step's ctx.*."
    ),
    ja=(
        "インラインに定義したアドホックなパイプラインをバックグラウンド"
        "起動し、即座に {status: started, run_id} を返す。専用のクラッ"
        "シュ復旧可能なドライバーセッションで実行され、最終結果は後で "
        "[pipeline] メッセージとして届く。'definition' は起動前に静的検"
        "証されるパイプライン DSL 文字列。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "run_pipeline": run_pipeline,
    "run_pipeline_async": run_pipeline_async,
    "run_pipeline_inline": run_pipeline_inline,
    "run_pipeline_inline_async": run_pipeline_inline_async,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────
#
# run_pipeline / run_pipeline_async share ONE parameters schema in the origin
# module (``_RUN_PIPELINE_PARAMETERS``); run_pipeline_inline /
# run_pipeline_inline_async likewise share ``_RUN_PIPELINE_INLINE_PARAMETERS``.
# Keyed here by the base verb name; the origin module reuses the same dict
# object for both the sync and async ToolDefinition.

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "run_pipeline": {
        "name": ParamDescription(
            text="The registered pipeline's name.",
            ja="登録済みパイプラインの名前。",
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
    },
    "run_pipeline_inline": {
        "definition": ParamDescription(
            text=(
                "The pipeline as a DSL string (Appendix B grammar): one or "
                "more '---'-separated YAML documents — exactly one 'pipeline:' "
                "document plus any 'schema:' documents its steps reference. "
                "Generated at call time; parsed + statically validated before "
                "anything runs."
            ),
            ja=(
                "DSL文字列としてのパイプライン（Appendix B 文法）。1つ以上の "
                "'---' 区切り YAML ドキュメント — 'pipeline:' ドキュメントが"
                "1つ必須、ステップが参照する 'schema:' ドキュメントは任意。"
                "呼び出し時に生成され、実行前にパースと静的検証を受ける。"
            ),
        ),
        "input": ParamDescription(
            text=(
                "Initial named context (ctx.*) for the pipeline's first step. "
                "Omit for a pipeline that needs no seed input."
            ),
            ja=(
                "パイプライン最初のステップ向けの初期名前付きコンテキスト"
                "（ctx.*）。シード入力が不要なパイプラインなら省略可。"
            ),
        ),
    },
}
