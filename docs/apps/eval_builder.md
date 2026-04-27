# eval_builder — アプリの評価スペックを自動生成する

既存アプリの DSL ファイルを読み込み、各フェーズの出力を評価するための `eval.md` スペックファイルを自動生成します。生成されたスペックは `reyn eval` コマンドで繰り返し実行できます。

---

## できること

- アプリの全フェーズに対してスキーマ検証と LLM 品質評価の基準を設計する
- テストケースを 1〜2 件自動設計する (通常ケース・レビューループ発生ケース)
- フェーズ間の一貫性チェック (例: plan フェーズで決めた名前が build フェーズでも同じか) を記述する
- 生成した `eval.md` をワークスペースに書き出し、実行コマンドを案内する

---

## 実行コマンド

```bash
reyn run \
  --app-dsl src/stdlib/apps/eval_builder/app.md \
  --dsl-root src/stdlib \
  --model openai/gemini-2.5-flash-lite \
  --input "評価スペックを作りたいアプリの DSL パス"
```

---

## 入力の書き方

ターゲットアプリの `app.md` パスを含む文章を入力します。

**書き方の例：**

```
dsl/apps/writing_review_app/app.md の eval.md を作って
```

```
dsl/apps/architecture_analyzer/app.md の評価スペックを作成してください。
記事の品質評価フェーズを重点的に見てほしい。
```

パスが不明瞭な場合は `ask_user` で確認を求めてくれます。

---

## フェーズの流れ

```
analyze_app  →  write_eval
```

| フェーズ | 役割 | やること |
|----------|------|----------|
| `analyze_app` | eval_designer | DSL ファイルを全件読み込み、フェーズごとの評価基準を設計する |
| `write_eval` | spec_writer | 設計した基準を `eval.md` 形式に整形してワークスペースに書き出す |

---

## 生成される eval.md の構造

```yaml
---
type: eval
app: dsl/apps/my_app/app.md
dsl_root: dsl/
judge_model: openai/gemini-2.5-flash-lite
---

## case: typical_case
input: "通常ケースのテスト入力"

### phase: analyze
schema:
- analysis_result.issues: array, min 1
- analysis_result.score: number, range 0.0-10.0

quality:
- issues の各項目が具体的な改善案を含んでいる

### cross_phase
- plan_app.app_name == build_app.app_name

### final
schema:
- app_name: string
- files_written: array, min 1

quality:
- summary がアプリの目的をユーザー視点で説明している
```

---

## 評価基準の種類

### schema (スキーマ検証)

アーティファクトのフィールド構造を決定論的にチェックします。LLM を使わないため高速・安定。

| 制約の例 | 意味 |
|----------|------|
| `field: string` | フィールドが文字列として存在する |
| `field: array, min 1` | 配列で 1 件以上ある |
| `field: number, range 0.0-10.0` | 数値が範囲内 |
| `field: boolean, equals true` | 値が true である |

### quality (品質評価)

LLM (judge_model) が内容を読んで判定します。

```
- issues の各項目が具体的な改善案を含んでいる
- summary がアプリの目的をユーザー視点で説明している
```

**`[aspirational]` タグ**: 満点を目指す基準ではなく、傾向把握のためのチェックには `[aspirational]` を付けます。スコアへの影響はなく参考値扱いになります。

```
- [aspirational] フィードバックが非常に具体的で実行可能な提案を含んでいる
```

---

## 出力ファイルと実行方法

```
workspace/eval_specs/{app_name}/eval.md
```

プロジェクトの DSL ディレクトリにコピーして実行します：

```bash
# ワークスペースから DSL ディレクトリにコピー
cp workspace/eval_specs/{app_name}/eval.md dsl/apps/{app_name}/eval.md

# 評価を実行
reyn eval --spec dsl/apps/{app_name}/eval.md --model openai/gemini-2.5-flash-lite
```

---

## 最終出力

```json
{
  "eval_md_path": "eval_specs/my_app/eval.md",
  "case_count": 2,
  "total_criteria": 18,
  "next_steps": "workspace/eval_specs/my_app/eval.md に書き出しました。..."
}
```

---

## Tips

- **全フェーズのアーティファクト .md を読んでから設計**: `analyze_app` は DSL ファイルを全件読んでからフィールド名を確定するため、存在しないフィールドへの参照が生まれにくい設計になっています
- **フィールド名は必ず DSL に従う**: スキーマ検証のフィールドパスが実際のアーティファクト定義と一致していないと評価エラーになります
- **レビューループがあるアプリはケース 2 が重要**: 最初の draft で revision が必要になるような入力を case 2 に設定することで、ループが正しく動くかを検証できます
- **app_improver と組み合わせると効果的**: `eval_builder` でスペックを作成 → `app_improver` で改善 → `eval` で効果測定、というサイクルで品質を上げていけます
