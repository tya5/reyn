---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, .reyn/config.yaml]
---

# 出力をローカライズする

**目的:** Skill 自体を変更せずに、選択した言語でテキストを生成させる。

## 言語の伝播方法

OS はすべてのコンテキストフレームに `output_language` フィールドを注入します。ユーザー向けテキストを生成する Phase の指示はそれに従います（「`{output_language}` で回答を書いてください」）。LLM は自動的にこの合図を拾います。Skill ごとのローカライゼーションコードは不要です。

## 解決順序

1. `--output-language` CLI フラグ（`reyn run`、`reyn eval`、`reyn chat`）
2. `.reyn/config.yaml`（個人設定のオーバーライド）
3. `reyn.yaml`（プロジェクト設定）
4. 組み込みデフォルト（`ja`）

## プロジェクトごとに設定する

```yaml
# reyn.yaml
output_language: en
```

このプロジェクトのすべてのランは、オーバーライドされない限り英語を使用します。

## セッションごとにオーバーライドする

```bash
reyn run my_skill "..." --output-language fr
reyn chat --output-language en
```

オーバーライドはそのランのみに影響します。

## Skill 作者向けガイダンス

Phase の指示に言語文字列をハードコードしないでください。代わりに `output_language` を参照します:

> `{output_language}` で返信してください。フレンドリーで簡潔なトーンで。

ランタイムは解決された値を代入します。これにより、1 つの Skill がモデルがサポートするすべての言語で機能します。

## これが行わないこと

- 入力を翻訳しません。ユーザーが日本語で入力した場合、LLM は日本語を見ます。`output_language` で返信するか入力言語をエコーするかは、プロンプトに依存します。
- モデルを選択しません。一部の言語ではより強力なモデルが必要な場合があります。`--model` で選択してください。
- 厳格な言語出力を強制しません。LLM はプレッシャー下で別の言語に滑り込む場合があります（信頼度の低い回答、コードブロック）。厳格な強制が重要な場合は、検証ステップを追加してください。

## 関連情報

- [リファレンス: reyn.yaml](../reference/config/reyn-yaml.md) — `output_language`
- [リファレンス: common-flags](../reference/cli/common-flags.md)
- [リファレンス: context-frame](../reference/runtime/context-frame.md)
