# app_improver — 既存アプリを自動改善する

実際にアプリを動かし、実行ログとアーティファクトを分析して、DSL ファイルへの具体的な改善案を生成・適用します。

---

## できること

- フェーズの実行回数・バリデーションエラー・信頼スコアなどを分析し、品質課題を特定する
- アーティファクトの内容を精査し、フィールドの欠落・内容の薄さを検出する
- フェーズ指示・アーティファクトスキーマへの具体的な修正案を生成する
- 改善ファイルをワークスペースに書き出し、適用手順を案内する

---

## 実行コマンド

```bash
agent-os run \
  --app-dsl src/stdlib/apps/app_improver/app.md \
  --dsl-root src/stdlib \
  --model openai/gemini-2.5-flash-lite \
  --allow-shell \
  --input "改善したいアプリの情報"
```

> **注意**: `run_target` フェーズがサブプロセスとしてターゲットアプリを実行するため、`--allow-shell` が必須です。

---

## 入力の書き方

以下の情報を自然言語で伝えます：

| 項目 | 必須 | 説明 |
|------|------|------|
| アプリの DSL パス | ◎ | `dsl/apps/{app_name}/app.md` の形式 |
| テスト入力 | ◎ | ターゲットアプリに渡す入力テキスト |
| 改善フォーカス | 任意 | レビューフェーズの品質向上など、重点箇所の指定 |
| モデル | 任意 | デフォルトは実行中のモデルと同じ |

**書き方の例：**

```
dsl/apps/writing_review_app/app.md を改善してほしい。
テスト入力: "AIの未来についての記事を書いて"
レビューフェーズのフィードバック品質を重点的に見てほしい。
モデルは openai/gemini-2.5-flash-lite を使用。
```

---

## フェーズの流れ

```
prepare → run_target → analyze_execution → plan_improvements → apply_improvements
```

| フェーズ | 役割 | やること |
|----------|------|----------|
| `prepare` | meta_coordinator | 入力を解析し、ターゲットアプリの実行パラメータを準備する |
| `run_target` | executor | ターゲットアプリをサブプロセスで実行し、ログとアーティファクトのパスを取得する |
| `analyze_execution` | quality_analyst | イベントログ・アーティファクト・DSL ファイルを精査し、品質スコアと具体的な問題点を特定する |
| `plan_improvements` | app_architect | 問題点ごとに具体的な DSL 修正内容を設計する |
| `apply_improvements` | implementer | 修正ファイルをワークスペースの `dsl_patches/` に書き出す |

---

## 出力と改善の適用方法

改善されたファイルはワークスペース内に書き出されます。実際の DSL ファイルへの適用は手動で行います。

```
workspace/dsl_patches/
  apps/{app_name}/
    phases/{phase_name}.md    ← 改善されたフェーズ定義
    artifacts/{name}.md       ← 改善されたアーティファクト定義
```

適用手順：

```bash
# パッチファイルを確認する
cat workspace/dsl_patches/apps/{app_name}/phases/{phase_name}.md

# 問題なければプロジェクトの DSL に上書きコピーする
cp workspace/dsl_patches/apps/{app_name}/phases/{phase_name}.md \
   dsl/apps/{app_name}/phases/{phase_name}.md
```

最終出力のサマリー例：

```json
{
  "files_modified": [
    "dsl_patches/apps/writing_review_app/phases/review.md → dsl/apps/writing_review_app/phases/review.md"
  ],
  "summary": "review フェーズの指示を具体化し、評価基準の明示と verdict フィールドの意味を追記した",
  "next_steps": "workspace/dsl_patches/ 内のファイルをレビューし、問題なければターゲットパスにコピーしてください"
}
```

---

## analyze_execution が見ているもの

実行ログ (JSONL) から以下を分析します：

| チェック項目 | 内容 |
|-------------|------|
| フェーズ訪問回数 | 多いほど LLM が迷っているサイン |
| `phase_retry` イベント | バリデーション失敗 → フェーズ指示の曖昧さが原因なことが多い |
| 信頼スコア (confidence) | 低いほど LLM が確信を持てていない |
| アーティファクトの内容充実度 | 必須フィールドの欠落・内容の薄さを検出 |
| `workflow_aborted` | 致命的エラーの有無 |

品質スコアが **8 以上** の場合は変更なし (空の `changes` 配列) を返します。

---

## Tips

- **テスト入力は本番に近いものを**: 単純すぎる入力だと問題が顕在化しないことがあります
- **改善フォーカスを指定すると精度が上がる**: 「レビューの品質」「アーティファクトのフィールド設計」など具体的に伝えましょう
- **改善後は eval_builder で定量評価**: `eval_builder` でテストスペックを作り、改善効果を数値で確認できます
- **ターゲットのワークスペースは自動生成される**: `workspace/target_runs/{app_name}/` に実行結果が保存されます
