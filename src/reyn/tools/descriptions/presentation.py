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

from reyn.tools.descriptions._types import ToolDescription

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
