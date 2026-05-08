# Postprocessor

**Postprocessor** は、Skill の終了時に LLM の最終出力と呼び出し元が受け取る artifact の間で実行される決定論的な変換です。

構造的には [Phase preprocessor](principles.md) と対称です — ステップ型、op セット、`on_error` セマンティクス、パーミッションゲートはすべて同一です。異なるのは **発火位置** のみです。Preprocessor は Phase 開始時に発火し、Postprocessor は Skill 終了時に発火します。

## Why

一部の Skill は「リッチな」呼び出し元向け artifact を生成します。その計算には LLM が決定するフィールドと決定論的に導出できるフィールドが混在しています。例を挙げます。

- ブログライター Skill は LLM から `{title, body}` を生成し、その後 `html_rendered`、`word_count`、`reading_minutes` を決定論的に計算する。
- コードレビュー Skill は LLM から `{severity, summary, suggestions}` を生成し、その後 Workspace から解決した `affected_files`、`tagged_owners` でエンリッチする。
- 要約 Skill は LLM から `{paragraphs}` を生成し、その後返却前に PII トークンをサニタイズする。

いずれのケースでも、LLM が決定論的フィールドの計算にトークンを費やすべきではなく、またそのためにフォローアップ Phase を追加する必要もありません。Postprocessor はそのための適切な場所です。

## 2 つの output schema

Postprocessor を持つ Skill は **2 つの** output schema を持ちます。

| Schema | 役割 | 宣言場所 |
|---|---|---|
| `output_schema`（既存） | LLM の finish contract — LLM が生成するもの | skill.md frontmatter |
| `postprocessor.output_schema`（新規） | 呼び出し元の contract — Skill が呼び出し元に返すもの | postprocessor ブロック内 |

パイプラインは次のとおりです。

```
LLM finish artifact（output_schema 準拠）
        ↓
[postprocessor steps]
        ↓
呼び出し元 artifact（postprocessor.output_schema 準拠）
        ↓
呼び出し元に返却
```

Postprocessor を持たない Skill は既存の `output_schema` のみを持ち、それが両方の contract を兼ねます（= LLM の contract = 呼び出し元の contract）。

## Preprocessor との対称性

| | preprocessor | postprocessor |
|---|---|---|
| 発火タイミング | Phase 開始時 | Skill 終了時 |
| 入力元 | 上流 Phase の出力（任意） | LLM の finish artifact（Skill `output_schema`） |
| 出力先 | Phase の `input_schema`（固定） | postprocessor の `output_schema`（固定） |
| ステップ型 | `validate` / `run_op` / `iterate` / `lint_plan` / `python` | 同一 |
| 実行可能 op | `run_skill` 可; `ask_user` 不可; LLM ステップなし | 同一（同等） |
| `on_error` ポリシー | ステップごとに `fail` / `skip` / `empty` | 同一（同等） |
| パーミッションゲート | `skill.permissions` | 同一（Skill レベルの宣言） |

ランナーは Preprocessor とロジックを共有しており、異なるのは流入する artifact、出力を検証する schema、および発火位置です。

## 宣言

```yaml
---
name: blog_writer
entry: draft
graph:
  draft: [review]
final_output: post                      # LLM contract（既存）
postprocessor:                          # 呼び出し元 contract（新規）
  output_schema: rendered_post          # artifact 名参照
  steps:
    - type: python
      module: ./rendering.py
      function: to_html
    - type: python
      module: ./rendering.py
      function: count_words
    - type: validate
      schema:
        type: object
        properties:
          word_count: { type: integer, minimum: 1 }
        required: [word_count]
---
```

`output_schema` にはインライン JSON Schema の dict リテラル、または Skill の artifact レジストリ内の artifact 名を参照する文字列を指定できます。stdlib での再利用を考えると artifact 名の形式が推奨されます。

## 失敗のセマンティクス

Postprocessor ステップには `on_error: fail | skip | empty` を宣言できます。Preprocessor と同一です。

- **`fail`**（デフォルト）: ステップの失敗は例外を発生させ Skill を中断します。Skill の中断は `WorkflowAbortedError` として記録され、ADR-0013 に従い Skill ごとの snapshot は削除されます（自動再開なし）。
- **`skip`**: 失敗はログに記録してスキップし、後続ステップは続行します。
- **`empty`**: 失敗するとステップの `into:` ターゲットに空の値を生成し、後続ステップは続行します。

コンテキスト上回復可能な失敗のステップ（あると便利だが必須ではないエンリッチメントなど）には `skip` / `empty` を使用してください。呼び出し元が不正な artifact を受け取らないよう、デフォルトは `fail` にしてください。

## Resume

Postprocessor のステップは Preprocessor および Phase の op と同じ `dispatch_tool` を通じて実行されるため、`step_completed` イベントを発行し、メモ化に参加します。Postprocessor の途中でクラッシュした場合:

1. Skill ごとの snapshot に `current_phase = "__post__"` が記録されます（予約済みの擬似 Phase）。
2. 自動再開が snapshot を読み込み、直接 Postprocessor のリプレイにジャンプし、メモ参照によって既にコミット済みのステップをスキップします。
3. World-purity op（= `file/read`、MCP 読み取り API）は ADR-0011 に従い再開時に再実行されます。

LLM の finish artifact は Postprocessor 開始前に Workspace に永続化されるため、インプロセス状態が失われても再開時に耐久性のある入力 artifact が確保されます。

## Postprocessor とフォローアップ Phase の使い分け

**Postprocessor** を使う場面:

- 変換が純粋に決定論的（LLM 呼び出しなし、ユーザー入力なし）。
- 出力が LLM の finish artifact から機械的に導出できる。
- 中間状態を LLM に見せる必要がない。

**フォローアップ Phase** を使う場面:

- 次のステップが LLM の判断を必要とする。
- 変換が失敗する可能性があり、LLM がリトライまたは説明すべき。
- 次のステップの出力を Phase の通常の schema チェックで検証したい。

Postprocessor は「仕上げ」「レンダリング」「バリデーション」「メトリクス出力」といった、LLM トークンでは高コストだが決定論的コードでは安価な作業のためのものです。

## スコープ外（延期）

- `retry` セマンティクスを持つ Postprocessor（= ステップ失敗が LLM 再呼び出しをトリガー）。未サポート。バリデーション失敗時に LLM 再呼び出しが必要な場合は、retry ポリシーを持つ Phase としてモデル化してください。
- Skill の外部でスタンドアロンのフックとして動作する Postprocessor。Postprocessor は Skill の contract に属します。
- Phase レベルの Postprocessor（= 「Phase 終了」フック）。不要です。次の Phase の Preprocessor が Phase 境界の変換をすでにカバーしています。

## 関連情報

- [Phase vs Skill vs OS](phase-vs-skill-vs-os.md) — Postprocessor が属するアーキテクチャレイヤー。
- [Permission model](permission-model.md) — `skill.permissions` が管理するもの。
- [Skill resume](skill-resume.md) — Postprocessor が統合する、より広い再開の仕組み。
