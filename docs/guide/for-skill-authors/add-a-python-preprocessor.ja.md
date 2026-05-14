---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md, reyn.yaml]
---

# Python preprocessor ステップを追加する

**目的:** LLM 呼び出しの前に Python 関数を実行し、決定論的に計算されたフィールド（統計、正規化、構造化パース）で入力 artifact をエンリッチする。

## 使うべき状況

- 計算が決定論的であり、毎回同じように実行したい。
- LLM には高コストまたはエラーが発生しやすい処理（数値統計、正規表現パース、JSON 形式変換）。
- プロンプトエンジニアリングコストを永続的に払うよりも、コードレビューコストを一度払う方がよい。

## 2 つのモード

| モード | サンドボックス | 用途 |
|------|------------|---------|
| `safe` | AST 検証、制限された builtins、allowlist インポート、subprocess | 標準的な数学/統計/正規表現処理 |
| `unsafe` | なし — 完全な Python | ファイル I/O、カスタムパッケージ、`safe` でブロックされるもの |

デフォルトは `safe` です。`safe` で本当に必要なものがブロックされる場合にのみ `unsafe` を使用してください。

## ステップ 1 — 関数を書く

`<skill_dir>/stats.py`:

```python
def compute(artifact):
    text = artifact["data"].get("text", "")
    return {"word_count": len(text.split())}
```

関数は入力 artifact を受け取り、JSON シリアライズ可能な dict を返します。

## ステップ 2 — Phase で宣言する

`phases/draft.md`:

```yaml
---
type: phase
name: draft
input: user_message
preprocessor:
  - python:
      module: stats
      function: compute
      mode: safe
      output_schema:
        type: object
        required: [word_count]
        properties:
          word_count: { type: integer }
      into: stats
---

`stats.word_count` を使用して、テキストを要約するか展開するかを判断してください。
```

`output_schema` は必須です。LLM は形状を知る必要があり、Reyn はユーザーコードをコンパイル時に実行して推論しません。

## ステップ 3 — Permission を宣言する

Phase の frontmatter に:

```yaml
permissions:
  python:
    - module: stats
      function: compute
      mode: safe
      timeout: 30
```

`module`/`function` は preprocessor ステップと一致する必要があります。

## ステップ 4 — 起動時に承認する

`safe` モードのステップも初回は承認が必要です:

```yaml
# reyn.yaml — プロジェクト全体で事前承認
permissions:
  python:
    safe: allow
```

`unsafe` の場合:

```yaml
permissions:
  python:
    unsafe: allow
```

…そして `--allow-untrusted-python` オプションで実行します。

## `safe` モードで禁止されること

- `open`、`eval`、`exec`、`__import__`、`compile`、`globals`、`locals`
- `subprocess` やその他の危険なモジュール
- キュレートされた allowlist（`math`、`statistics`、`json`、`re`、`random`、`time`、`datetime` など）以外のインポート

allowlist は `reyn.yaml` の `permissions.python.allowed_modules` で拡張できます。

## 関連情報

- [リファレンス: preprocessor](../../reference/dsl/preprocessor.md) — `python` ステップ
- [リファレンス: permissions](../../reference/config/permissions.md) — `python` 宣言
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions.python`
