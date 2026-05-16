# SP レンダリング確認 CLI (`scripts/dogfood_sp_render.py`)

スキル開発中にシステムプロンプトの出力を確認するための CLI。OS が LLM コールに実際に注入する内容をプレビューするためにその都度書いていた `python -c "..."` ワンライナーの置き換えです。

## なぜこのツールが必要か

このツールが存在する以前は、システムプロンプトを確認するには毎回アドホックな Python スニペットが必要でした。SP ビルダーをインポートし、フェイクコンテキストを構築し、render を呼び、出力する。確認したい内容によってスニペットの形が変わり (サイズ? セクション一覧? legacy リテラル監査?)、保存もされませんでした。1 セッション中にほぼ同じ使い捨てスクリプトを 5 種類書くことも珍しくありませんでした。

`dogfood_sp_render.py` はそのワークフローを 6 つの named mode に標準化します。エージェントの設定を表すフラグを渡し、mode を選ぶだけで、一貫した再現可能な出力が得られます。

## セットアップ

プロジェクト標準の依存関係以外に追加インストールは不要です。スクリプトは `scripts/` に置かれており、仮想環境内の既存モジュールのみを使用します:

```bash
# プロジェクトルートから
python scripts/dogfood_sp_render.py [flags] [mode]
```

## 使い方

### デフォルト mode — SP 全文プレビュー

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --agent-role "コーディングアシスタント" \
  --skill "skill_builder=スキルをビルド・改善する" \
  --skill "eval=評価シナリオを実行する"
```

レンダリングされたシステムプロンプトを stdout に出力します。長い SP は `less` にパイプしてください。

### `--stats` — 文字数・行数の確認

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=スキルをビルド・改善する" \
  --stats
```

出力:

```
2635 chars / 47 lines
```

開発中の SP サイズ監視に使います。コミット後にサイズが急増した場合は意図しない injection が発生している可能性があります。

### `--show-sections` — SP 構造の概要確認

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=スキルをビルド・改善する" \
  --show-sections
```

出力:

```
## Capabilities (routing guide)
## Action categories
## Behaviour
```

SP 全文を読まずに期待するセクションが存在することを確認する際に使います。

### `--grep-legacy` — legacy リテラルの監査

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=スキルをビルド・改善する" \
  --grep-legacy
```

legacy リテラルが見つからない場合は exit code 0 で終了します。見つかった場合は exit code 1 で終了し、問題のある行を出力します。pre-commit hook や CI チェック向けに設計されています — [ワークフローとの統合](#ワークフローとの統合)を参照してください。

### `--compare-legacy` — wrapper SP と legacy SP のサイズ比較

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=スキルをビルド・改善する" \
  --compare-legacy
```

出力:

```
Legacy: 9,029 chars / Wrapper: 2,635 chars / Reduction: 70.8%
```

dogfood prep 中に wrapper SP が legacy SP より十分小さいことを確認するために使います。regression (wrapper が legacy より大きい、または削減率が 50% 未満) は意図しない injection の調査シグナルです。

### `--legacy-check` — legacy mode の pass/fail 判定

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=スキルをビルド・改善する" \
  --legacy-check
```

stdout に `PASS` または `FAIL` を出力し、対応する exit code で終了します。pass/fail の状態だけが必要な場合は `--grep-legacy` より軽量な代替手段です。

## フラグリファレンス

### エージェント識別フラグ

| フラグ | 説明 |
|--------|------|
| `--agent-name NAME` | エージェント名 (SP の冒頭に使用) |
| `--agent-role TEXT` | SP に注入するロール説明 |

### 機能フラグ (繰り返し指定可)

| フラグ | 説明 | 形式 |
|--------|------|------|
| `--skill name=desc` | このエージェントが使用できるスキル | `name=description` |
| `--agent-peer name=role` | 委任可能なピアエージェント | `name=role` |
| `--mcp-servers name=desc` | アクセス可能な MCP サーバ | `name=description` |
| `--indexed-sources name` | インデックス済みソース名 (RAG 対応エージェント向け) | 単純な名前 |

### スコープとコンテキストフラグ

| フラグ | 説明 |
|--------|------|
| `--file-scope read=path write=path` | パーミッション対応レンダリング用のファイルスコープ |
| `--output-language LANG` | 出力言語コード (例: `ja`、`en`) |
| `--project-context TEXT` | SP に注入するプロジェクトコンテキスト |

### レンダリングモードフラグ

| フラグ | 説明 |
|--------|------|
| `--hide-legacy-tools` | legacy ツール定義を非表示にしてレンダリング |
| `--universal-wrappers-enabled` | universal wrapper mode を有効化 |

### 出力モードフラグ (排他的)

| フラグ | 説明 |
|--------|------|
| `--stats` | 文字数・行数のみ出力 |
| `--show-sections` | セクションヘッダーのみ出力 |
| `--grep-legacy` | legacy リテラルを監査。発見時は exit 1 |
| `--compare-legacy` | wrapper と legacy のサイズ比較を出力 |
| `--legacy-check` | PASS/FAIL を出力して対応 exit code で終了 |

## ワークフローとの統合

### コミット前 — リーク確認

`--grep-legacy` を実行して、wrapper-only mode に routing を壊す legacy リテラルが含まれていないことを確認します:

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --universal-wrappers-enabled \
  --skill "skill_builder=スキルをビルド・改善する" \
  --grep-legacy
```

exit code 1 はリテラルがリークしていることを意味します。出力された行を確認してソースを特定してください。

### dogfood prep 中 — サイズデルタの観察

各 dogfood batch の前に削減率を確認してベースラインを確立します。コード変更後に削減率が 50% を下回った場合は、高コストな LLM セッションを走らせる前に調査する価値があります:

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=スキルをビルド・改善する" \
  --compare-legacy
```

### routing 問題のデバッグ時

`--show-sections` で期待するセクションがすべて存在することを確認し、次に LLM が誤動作している特定のセクションについて全文を読みます:

```bash
# 1. セクション確認
python scripts/dogfood_sp_render.py --agent-name my_agent --skill "..." --show-sections

# 2. 全文を読む
python scripts/dogfood_sp_render.py --agent-name my_agent --skill "..." | less
```

### 使うべきでない場面

- **実際のライブセッションが LLM に送信した内容を確認したい場合。** このツールは指定したフラグから SP をレンダリングするものであり、実行中のセッションの状態を読み取るものではありません。そのためには `REYN_LLM_TRACE_DUMP` + `dogfood_trace.py` を使います。
- **LLM の挙動を監査したい場合。** SP レンダリングは入力側のみです。出力側の挙動には `llm_replay.py` を使います。

## 関連リソース

- [LLM ペイロードトレース](dogfood-tracing.md) — ライブ LLM ペイロードのキャプチャと検査 (`REYN_LLM_TRACE_DUMP`、`dogfood_trace.py`、`llm_replay.py`)
