---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn run]
---

# `reyn run`

Skill をエンドツーエンドで実行します。

## 概要

```
reyn run [OPTIONS] [SKILL] [INPUT]
```

## 位置引数

| 名前 | 説明 |
|------|-------------|
| `SKILL` | Skill 名。順番に解決されます: `reyn/project/<name>` → `reyn/local/<name>` → `src/stdlib/skills/<name>`。 |
| `INPUT` | 初期入力。JSON 文字列はそのまま使用されます（有効な artifact でなければなりません）。自然言語の文字列は `{"type": "user_message", "data": {"text": "..."}}` として自動ラップされます。省略した場合は stdin から読み取ります。 |

## オプション

| フラグ | 説明 |
|------|-------------|
| `--skill-path DIR` | Skill ディレクトリへのパス（名前解決をオーバーライド）。 |
| `--module MODULE` | `skill` オブジェクトを公開する Python モジュールパス。 |
| `--skill-root DIR` | 共有 artifact/Phase 解決のための Skill ツリーのルート。`--skill-path` 使用時は自動推論されます。推論が誤っている場合にオーバーライドしてください。 |
| `--model MODEL` | モデルクラス（`light` / `standard` / `strong`）または LiteLLM モデル文字列。`reyn.yaml` の `models` マップを通じて解決されます。 |
| `--output-language LANG` | 出力言語コード。デフォルトは `reyn.yaml` から。 |
| `--max-phase-visits N` | ランごとの単一 Phase 再訪問の上限。`0` = 無制限。デフォルト `25`。 |
| `--events` | 実行後に完全なイベントログを表示。 |
| `--strict` | すべてのネスト深さで必須フィールドを強制します（デフォルト: トップレベルのみ）。 |
| `--allow-shell` | `shell` Control IR op を有効にする。デフォルトはオフ。 |
| `--allow-unsafe-python` | unsafe モードの Python preprocessor ステップを有効にする（AST サンドボックスなし）。`--allow-untrusted-python` は後方互換性のためのレガシーエイリアスです。 |

## 例

自然言語入力で stdlib Skill を実行:

```bash
reyn run text_summarizer "reyn is a workflow OS for LLMs."
```

構造化 JSON 入力で実行:

```bash
reyn run my_skill '{"type": "topic_input", "data": {"topic": "ml"}}'
```

stdin から実行:

```bash
echo "summarize this text" | reyn run text_summarizer
```

実行後にイベントをリプレイ:

```bash
reyn run text_summarizer "..." --events
```

シェルアクセスが必要なメタ Skill を実行:

```bash
reyn run skill_improver "improve my_skill" --allow-shell
```

## 関連情報

- [リファレンス: skill.md frontmatter](../dsl/skill-md.md)
- `reference/runtime/events.md` — イベントの種類
- [コンセプト: architecture](../../concepts/architecture.md)
