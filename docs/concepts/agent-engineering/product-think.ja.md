---
type: concept
topic: architecture
audience: [human, agent]
---

# Product Think

製品としての agent の視点: 使用感、実行コスト、実環境での予測可能性。研究上の問題ではないため、投資が不足しがちですが、システムを長期間維持するかどうかを決めるのはこの部分です。

## Reyn の実装方法

### CLI の使い勝手

Reyn CLI は 1 つのモノリシックなエントリーポイントではなく、小さく組み合わせ可能なサブコマンドとして構成されています:

| コマンド | 目的 |
|---------|---------|
| `reyn run` | Skill をエンドツーエンドで実行 |
| `reyn eval` | eval スペックを実行 |
| `reyn lint` | Skill をリント（グラフ、frontmatter、Python AST） |
| `reyn chat` | ルーター + Memory を持つインタラクティブ REPL |
| `reyn init` | `reyn.yaml` と `.reyn/` をスキャフォールド |
| `reyn skills` | 利用可能な Skill の一覧、詳細表示 |
| `reyn permissions` | 保存された承認の確認/取り消し |
| `reyn memory` | Memory の一覧/表示/編集/検索/エクスポート |
| `reyn events` | 保存されたイベントログのリプレイ |
| `reyn config` | 設定の表示/編集 |

各コマンドは独立して習得できます。同じ `reyn.yaml` と `.reyn/` 状態ディレクトリを共有することで組み合わせられます。

### コスト規律

フラグまたは設定として表示される 3 つのレバー:

- **モデルクラス（`light` / `standard` / `strong`）。** Skill は特定のモデルを指定せずに書かれます。リゾルバーがクラスを `reyn.yaml` の具体的な LiteLLM モデル文字列にマッピングします。プロジェクトごと（または `--model` でラン単位）のコスト tier を切り替えるのは 1 行の変更です。eval はイテレーション中は `light` で実行し、最終的な採点には `strong` で実行できます。
- **ランごとのコストレポート。** `reyn run` と `reyn eval` は最終行にトークン使用量と USD コストを出力します。eval レポートはケースごとのコストを永続化するため、コストの後退は品質の後退と同じ場所で現れます。
- **`limits.phase.max_visits` と `limits.phase.max_wall_seconds`。** 暴走ループと Phase ごとの時間バジェットを制限します。どちらもコストの上限です（各訪問は少なくとも 1 回の LLM 呼び出しであり、時間制限のある Phase は低速 LLM による爆発的なコスト増を防ぎます）。

### 予測可能な UX

積み重なって効果を生む小さな選択がいくつかあります:

- **`output_language`。** 1 つの設定キーがすべての Skill にわたってユーザー向け出力の言語を制御します。Skill ごとのローカライゼーションコードは不要です。
- **`--events` / `--conversation`。** ランが予期しないことをした場合、記録の artifact は 1 回の CLI 呼び出しで取得できます。
- **状態はディスク上に存在する。** `.reyn/` にはイベント、チャット、eval レポート、承認、Memory が格納されます。重要なものはプロセスメモリだけに存在しません。

### プログラミングなしの組み合わせ

このシステムは関数ではなく Skill で考えることを促します。`chat` はルーター Skill であり、eval は judge Skill を繰り返す Skill であり、importer/improver/builder はそれ自体 Skill です。新しい高レベルのケイパビリティは、新しい CLI サブコマンドではなく新しい Skill になる傾向があります。

## まだ薄い部分

いくつかの UX/コストレバーが欠けているか、薄い状態です:

- **ストリーミング出力なし。** 長時間実行される Phase は完了するまでコンソールに何も表示されません（イベントログはリアルタイムで満たされますが、レンダリング出力は Phase ごとです）。インタラクティブな作業ではこれで問題ありません。非常に長時間実行される Skill では問題になります。
- **コストダッシュボードや傾向ビューなし。** ランごとのコストは表示されます。ラン間の集計はユーザーの作業です（データは他のツールにフィードするのに十分な構造を持っています）。
- **オンボーディングに粗い部分あり。** `reyn init` は設定をスキャフォールドしますが、チュートリアル 01 が実際のオリエンテーションです。単一の統合された `reyn quickstart` は存在しません。

これらはすべて OS を変更せずに対処できます。すでに安定したランタイム上のプロダクトポリッシュです。

## 関連情報

- [リファレンス: cli/run](../../reference/cli/run.md)
- [リファレンス: cli/eval](../../reference/cli/eval.md)
- [リファレンス: cli/chat](../../reference/cli/chat.md)
- [リファレンス: cli/common-flags](../../reference/cli/common-flags.md)
- [ハウツー: 出力のローカライゼーション](../../guide/for-skill-authors/ux-polish/localize-output.md)
- [evaluation-and-observability.md](evaluation-and-observability.md) — 収集されるコストデータ
- [retrieval-engineering.md](retrieval-engineering.md) — chat Memory が UX に直接影響する
