---
type: concept
topic: architecture
audience: [human, agent]
---

# Product Think

> **状態: stale。** このページは削除済みの phase-graph skill engine を前提に書かれています —
> 以下の CLI 表(`reyn run-once`、ワークフロー中心のサブコマンド)、
> `limits.phase.max_visits`、「まだ薄い部分」の「ストリーミング出力なし」/
> 「コストダッシュボードなし」という主張は、すべて現行の inline CUI(agents/cost/permissions
> のライブ audit chip 付き — `docs/feature-map.md` の Inline CUI セクションで現行確認済み、
> 以下の「ストリーミング出力なし」という主張と矛盾)より前のものです。
> 書き直しは follow-up として追跡されています。現行の grounded なストーリーは
> [`docs/concepts/architecture/charter.md`](../architecture/charter.md)
> (Product Think 行、7 feature family 全体で populate 済み)を参照してください。

製品としての agent の視点: 使用感、実行コスト、実環境での予測可能性。研究上の問題ではないため、投資が不足しがちですが、システムを長期間維持するかどうかを決めるのはこの部分です。

## Reyn の実装方法

### CLI の使い勝手

Reyn CLI は 1 つのモノリシックなエントリーポイントではなく、小さく組み合わせ可能なサブコマンドとして構成されています:

| コマンド | 目的 |
|---------|---------|
| `reyn run-once` | 汎用エージェントを stdin プロンプトで一度だけ実行 |
| `reyn chat` | ルーター + Memory を持つインタラクティブ REPL |
| `reyn agent` | 名前付き永続エージェントの作成/管理 |
| `reyn init` | `reyn.yaml` と `.reyn/` をスキャフォールド |
| `reyn permissions` | 保存された承認の確認/取り消し |
| `reyn memory` | Memory の一覧/表示/編集/検索/エクスポート |
| `reyn events` | 保存されたイベントログのリプレイ |
| `reyn config` | 設定の表示/編集 |

各コマンドは独立して習得できます。同じ `reyn.yaml` と `.reyn/` 状態ディレクトリを共有することで組み合わせられます。

### コスト規律

フラグまたは設定として表示される 3 つのレバー:

- **モデルクラス(`light` / `standard` / `strong`)。** ワークフローは特定のモデルを指定せずに書かれます。リゾルバーがクラスを `reyn.yaml` の具体的な LiteLLM モデル文字列にマッピングします。プロジェクトごと(または `--model` でラン単位)のコスト tier を切り替えるのは 1 行の変更です。
- **エージェントごとのコストレポート。** `reyn chat` の `/cost` スラッシュコマンドで現在のエージェントのトークン使用量 + USD コストをすぐ確認できます。
- **`limits.phase.max_visits` と `limits.phase.max_wall_seconds`。** 暴走ループと Phase ごとの時間バジェットを制限します。どちらもコストの上限です(各訪問は少なくとも 1 回の LLM 呼び出しであり、時間制限のある Phase は低速 LLM による爆発的なコスト増を防ぎます)。

### 予測可能な UX

積み重なって効果を生む小さな選択がいくつかあります:

- **`output_language`。** 1 つの設定キーがすべてのワークフローにわたってユーザー向け出力の言語を制御します。ワークフローごとのローカライゼーションコードは不要です。
- **`reyn events`。** ランが予期しないことをした場合、記録の artifact は 1 回の CLI 呼び出しで取得できます。
- **状態はディスク上に存在する。** `.reyn/` にはイベント、チャット、承認、Memory が格納されます。重要なものはプロセスメモリだけに存在しません。

### プログラミングなしの組み合わせ

このシステムは関数ではなくワークフローで考えることを促します。`chat` はルーターワークフローであり、importer/improver/builder はそれ自体ワークフローです。新しい高レベルのケイパビリティは、新しい CLI サブコマンドではなく新しいワークフローになる傾向があります。

## まだ薄い部分

いくつかの UX/コストレバーが欠けているか、薄い状態です:

- **ストリーミング出力なし。** 長時間実行される Phase は完了するまでコンソールに何も表示されません（イベントログはリアルタイムで満たされますが、レンダリング出力は Phase ごとです）。インタラクティブな作業ではこれで問題ありません。非常に長時間実行されるワークフローでは問題になります。
- **コストダッシュボードや傾向ビューなし。** ランごとのコストは表示されます。ラン間の集計はユーザーの作業です（データは他のツールにフィードするのに十分な構造を持っています）。
- **オンボーディングに粗い部分あり。** `reyn init` は設定をスキャフォールドしますが、チュートリアル 01 が実際のオリエンテーションです。単一の統合された `reyn quickstart` は存在しません。

これらはすべて OS を変更せずに対処できます。すでに安定したランタイム上のプロダクトポリッシュです。

## 関連情報

- [リファレンス: cli/chat](../../reference/cli/chat.md)
- [リファレンス: cli/common-flags](../../reference/cli/common-flags.md)

- [retrieval-engineering.md](retrieval-engineering.md) — chat Memory が UX に直接影響する
