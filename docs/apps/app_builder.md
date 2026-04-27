# app_builder — アプリを自然言語から生成する

自然言語のリクエストを受け取り、Agent OS で動作する新しいアプリの DSL ファイル一式を自動生成します。

---

## できること

- アプリの目的を日本語や英語で伝えるだけで、フェーズ・アーティファクト・グラフ構造を設計してくれる
- 生成されたファイルはそのまま `reyn run` で実行できる状態になる
- レビューループが必要かどうかを自動判断し、適切なフェーズ構成を選択する

---

## 実行コマンド

```bash
reyn run \
  --app-dsl src/stdlib/apps/app_builder/app.md \
  --dsl-root src/stdlib \
  --model openai/gemini-2.5-flash-lite \
  --input "作りたいアプリの説明"
```

生成されたファイルはワークスペース内の `dsl/apps/{app_name}/` に書き出されます。

---

## 入力の書き方

自然言語でアプリの目的・機能を説明します。

**最低限必要な情報：**
- 何をするアプリか
- 入力と出力のイメージ

**書き方の例：**

```
ブログ記事を自動生成するアプリを作ってほしい。
テーマを入力すると、記事の下書きを生成し、品質レビューを経て最終版を出力する。
```

```
顧客からのフィードバックテキストを受け取り、
ポジティブ・ネガティブ・提案の3カテゴリに分類して要約するアプリ。
```

アプリ名を迷っているときは「名前の候補をいくつか出して」と書くと候補を提示してくれます。

---

## フェーズの流れ

```
plan_app  →  build_app
```

| フェーズ | 役割 | やること |
|----------|------|----------|
| `plan_app` | app_architect | アプリ構造を設計。フェーズ・アーティファクト・遷移グラフを決定 |
| `build_app` | dsl_writer | 設計に基づいて DSL ファイルを生成・書き込み |

---

## 出力ファイル構成

生成されるファイルはワークスペース内に以下の構成で書き出されます：

```
workspace/dsl/apps/{app_name}/
  app.md                   ← アプリ定義 (エントリーフェーズ, グラフ, 最終出力)
  phases/
    {phase_name}.md        ← 各フェーズの定義
  artifacts/
    {artifact_name}.md     ← 各アーティファクトのスキーマ
```

---

## 最終出力

```json
{
  "app_name": "my_app",
  "app_path": "dsl/apps/my_app",
  "files_written": [
    "dsl/apps/my_app/app.md",
    "dsl/apps/my_app/phases/analyze.md",
    "dsl/apps/my_app/artifacts/analysis_result.md"
  ],
  "file_count": 5,
  "summary": "ユーザーが〜できるアプリ"
}
```

---

## 生成後の使い方

1. 生成されたファイルをプロジェクトの `dsl/` ディレクトリにコピーする

   ```bash
   cp -r workspace/dsl/apps/{app_name} dsl/apps/
   ```

2. リンターで整合性を確認する

   ```bash
   reyn lint --dsl dsl/
   ```

3. 実際に動かしてみる

   ```bash
   reyn run --app-dsl dsl/apps/{app_name}/app.md --dsl-root dsl/ --input "テスト入力"
   ```

---

## フェーズ設計パターン

`plan_app` は入力の性質に応じて以下のパターンから最適なものを選びます：

| パターン | 構成 | 向いているケース |
|----------|------|-----------------|
| A: レビューループ | generate → review → deliver | コンテンツ生成など主観的判断が必要な場合 |
| B: 調査→生成 | research → generate → review → deliver | 情報収集が先に必要な場合 |
| C: シンプル線形 | process → deliver | 決定論的な変換・分類など |

---

## Tips

- **入力が曖昧でも大丈夫**: 不足している情報は `ask_user` で確認を求めてくれます
- **生成後は必ずリンターを回す**: `reyn lint` で未定義のアーティファクト参照などを検出できます
- **品質が低い場合は app_improver で改善できます**: 生成されたアプリを `app_improver` に渡すとフェーズ指示を自動改善してくれます
