"""Tool descriptions for the ``presentation`` category.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): ``present`` (the present-layer
arc, #2692 / FP-0054 / FP-0055) and ``render_template`` (#2692 / FP-0055),
both chat + pipeline invocation surfaces onto pre-existing op handlers.
Each ``.text`` value is copied verbatim from its origin tool module; the
origin module now aliases its ``_X_DESCRIPTION`` constant to
``presentation.NAME.text``.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

present = ToolDescription(
    tool_name="present",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Show bulk tool-result data to the user directly (zero-token "
        "offload) instead of the LLM re-typing it through its own output "
        "tokens — the present-layer arc's chat + pipeline entry point."
    ),
    text=(
        "Show bulk data to the user directly, WITHOUT re-typing it through your own output tokens. "
        "Use this after a tool returns a large result (a file, a query result, a list) that the user "
        "should see: pass a reference to it and the presentation layer renders it for them. "
        "Simple path — just pass 'data_ref' (a zone-readable path or an offloaded result ref) and it is "
        "shown with a sensible default view; you do not need to design anything. Optionally pass 'view' "
        "(a registered presentation name) to use a named layout, or 'blueprint' (advanced: a declarative "
        "component tree) for full control — at most one of the two. Pass 'data_inline' INSTEAD of "
        "'data_ref' only for small data already in your context (exactly one of data_ref / data_inline). "
        "Reading a 'data_ref' requires the same file-read permission as reading the file. Returns a "
        "compact acknowledgement (what reached the user and whether the view bound), not the data itself."
    ),
    ja=(
        "大量データを、自分の出力トークンで再入力することなく直接ユーザ"
        "ーに表示する。ツールが大きな結果（ファイル、クエリ結果、リスト"
        "など）を返した後、参照を渡すだけでプレゼンテーション層が表示"
        "してくれる。'data_ref' を渡すだけの簡易パスがデフォルト。"
    ),
)

render_template = ToolDescription(
    tool_name="render_template",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Render a Jinja2 template against structured data into a plain "
        "string, as a sandboxed producer with no sink of its own — the "
        "caller routes the result wherever it wants (present / write / "
        "downstream pipeline step)."
    ),
    text=(
        "Render a Jinja2 template against structured data into a plain string. A sandboxed producer: it "
        "writes nothing and shows nothing — it returns the rendered text, which you then route wherever "
        "you want (show it with 'present', write it with a file step, or pass it downstream in a "
        "pipeline). Provide exactly one template source (inline 'template' text, or 'template_ref' — a "
        "zone-readable template file path) and exactly one data source ('data_inline' — small data in "
        "your context, or 'data_ref' — a zone-readable path). The data binds under 'data' in the template "
        "(e.g. {{ data.results[0].title }}). 'undefined' controls unbound variables: 'strict' (default) "
        "errors naming the missing name; 'lenient' renders them empty and reports them. Reading a "
        "template_ref / data_ref requires the same permission as reading the file."
    ),
    ja=(
        "Jinja2 テンプレートを構造化データに対してレンダリングし、プレー"
        "ンな文字列を返す。書き込みも表示もしないサンドボックス化された"
        "プロデューサー — レンダリング結果は present / ファイル書き込み / "
        "パイプラインの後段など、呼び出し側が自由にルーティングする。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "present": present,
    "render_template": render_template,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "present": {
        "data_ref": ParamDescription(
            text=(
                "What to show: a zone-readable path or an offloaded-result reference. Read under the "
                "same authority as file.read. Provide exactly one of data_ref / data_inline."
            ),
            ja=(
                "表示するもの: ゾーン読み取り可能なパス、またはオフロードされた"
                "結果への参照。file.read と同じ権限で読む。data_ref / "
                "data_inline のどちらか一方のみ指定する。"
            ),
        ),
        "data_inline": ParamDescription(
            text=(
                "Small data already in your context, passed directly instead of a ref. Provide "
                "exactly one of data_ref / data_inline."
            ),
            ja=(
                "既にコンテキストにある小さなデータを参照の代わりに直接渡す。"
                "data_ref / data_inline のどちらか一方のみ指定する。"
            ),
        ),
        "view": ParamDescription(
            text=(
                "Optional: the name of a registered presentation (presentations.yaml) to render "
                "with. Omit for the default view. At most one of view / blueprint."
            ),
            ja=(
                "任意: レンダリングに使う登録済みプレゼンテーション名"
                "（presentations.yaml）。省略時はデフォルトビュー。view / "
                "blueprint はどちらか一方のみ。"
            ),
        ),
        "blueprint": ParamDescription(
            text=(
                "Optional (advanced): an inline declarative component tree with JSON-Pointer "
                "bindings for full control over the layout. Prefer the simple default (omit both "
                "view and blueprint) unless you specifically need a custom layout. At most one of "
                "view / blueprint."
            ),
            ja=(
                "任意（上級者向け）: レイアウトを完全制御するインラインの"
                "宣言的コンポーネントツリー（JSON-Pointer バインディング付き）。"
                "カスタムレイアウトが特に必要でない限り、view/blueprint 両方"
                "省略のシンプルなデフォルトを推奨。view / blueprint はどちらか"
                "一方のみ。"
            ),
        ),
    },
    "render_template": {
        "template": ParamDescription(
            text=(
                "Inline Jinja2 source text. Provide exactly one of template / template_ref."
            ),
            ja="インラインの Jinja2 ソーステキスト。template / template_ref のどちらか一方のみ指定する。",
        ),
        "template_ref": ParamDescription(
            text=(
                "A zone-readable path to a template file (read as raw source text under file.read "
                "authority). Provide exactly one of template / template_ref."
            ),
            ja=(
                "テンプレートファイルへのゾーン読み取り可能なパス（file.read "
                "権限で生テキストとして読む）。template / template_ref の"
                "どちらか一方のみ指定する。"
            ),
        ),
        "data_ref": ParamDescription(
            text=(
                "A zone-readable path whose value binds under 'data' in the template. Read under the "
                "same authority as file.read. Provide exactly one of data_ref / data_inline."
            ),
            ja=(
                "値がテンプレート内の 'data' にバインドされるゾーン読み取り"
                "可能なパス。file.read と同じ権限で読む。data_ref / "
                "data_inline のどちらか一方のみ指定する。"
            ),
        ),
        "data_inline": ParamDescription(
            text=(
                "Small data already in your context, bound under 'data' in the template. Provide "
                "exactly one of data_ref / data_inline."
            ),
            ja=(
                "既にコンテキストにある小さなデータをテンプレート内の 'data' "
                "にバインドする。data_ref / data_inline のどちらか一方のみ"
                "指定する。"
            ),
        ),
        "undefined": ParamDescription(
            text=(
                "Undefined-variable policy. 'strict' (default) errors naming the missing variable; "
                "'lenient' renders undefined as empty and reports the unbound names."
            ),
            ja=(
                "未定義変数のポリシー。'strict'（デフォルト）は不足変数名を"
                "示すエラーを出す。'lenient' は未定義を空としてレンダリングし"
                "未束縛の名前を報告する。"
            ),
        ),
    },
}
