---
type: concept
topic: architecture
audience: [human, agent]
---

# Product Think

製品としての agent の視点: 使用感、実行コスト、実環境での予測可能性。研究上の問題ではないため、投資が不足しがちですが、システムを長期間維持するかどうかを決めるのはこの部分です。

## Reyn の実装方法

### CLI の使い勝手

Reyn の CLI は 1 つのモノリシックなエントリーポイントではなく、小さく組み合わせ可能なサブコマンドとして構成されています — それぞれが自身のサブシステムの operator サーフェス(agent / topology / memory / permissions / events / mcp / config / …)だけを所有し、共有メガコマンドではなく同じ `reyn.yaml` と `.reyn/` 状態ディレクトリを共有します。重複したリストをここに載せる代わりに、完全で現行のコマンド一覧は [feature-map.md の CLI セクション](../../feature-map.md#cli) を参照してください。

### ライブな legibility: inline CUI の audit chip

inline CUI のステータス chip バー(Agents / Cost / Model / Tools / MCP / Skills / Hooks / Pipes / Cron / Tasks)は、P6 audit-event ログが記録するのと同じ operator に見える状態を、事後のリプレイでのみ利用可能なのではなく、ライブかつインラインで表面化します — これは Observability レンズが同じ chip をどう読むかの dual-facet な相方です([observability.md](observability.md) を参照)。

### コストレポートと削減 — bounding とは別物

似ているがレンズとしては別物の 2 つ:

- **コストの *レポート*(このレンズ)**: `/cost` は現在の agent のトークン + USD の簡易サマリーを、`/budget` は完全な内訳を表示します。`cost_warn` は、解決されたモデルの 100 万トークンあたりコストが閾値を超えたときにオペレーターへ事前選択の警告を出す仕組みで、モデル・セッションごとに重複排除されます — legibility と predictability であり、それ以上のものではありません。
- **コストの *bounding*(cross-cutting band の `cost/budget` メンバーであり、このレンズではない)**: 超過すると以降の支出を拒否する、agent ごと/日次/月次のハードなトークン+USD キャップ。bounding cap を Product Think の exemplar として引用しないでください — それは band の仕事であり、このレンズの仕事ではありません(band↔lens の完全な区別は `CLAUDE.md` の Constitution 節を参照)。
- **コストの *削減*** はこのレンズのもう1つの facet です: `present` はバルクデータを LLM 出力として再現する代わりに、ほぼ 0 出力トークンでサーフェスにルーティングします — 単なるレポート機構ではなく、本物のトークンコスト削減です。

### 予測可能な UX

- **`output_language`。** 1 つの設定キーがユーザー向け出力の言語を制御します。agent ごとのローカライゼーションコードは不要です。
- **`reyn events`。** ランが予期しないことをした場合、記録の artifact は 1 回の CLI 呼び出しで取得できます。
- **状態はディスク上に存在する。** `.reyn/` にはイベント、チャット、承認、Memory が格納されます。重要なものはプロセスメモリだけに存在しません。
- **On-limit mode。** `interactive` / `auto_extend` / `unattended` は、あらゆる loop/timeout/budget チェックポイントに対して、オペレーターに予測可能で config で選択可能な制御を一様に与えます — [reliability-engineering.md](reliability-engineering.md) を参照。
- **`/agents` view。** 実行中の agent/session を一覧表示し attach できる — skill run と delegate されたピアにまたがる、オーケストレーションされた作業への operator legibility。

## まだ薄い部分

- **コストダッシュボードや傾向ビューなし。** ランごとのコストは表示されます(`/cost`、`/budget`)。ラン間の集計はオペレーター自身の作業です(データは他のツールにフィードするのに十分な構造を持っています)。
- **オンボーディングに粗い部分あり。** `reyn init` は設定をスキャフォールドしますが、getting-started ガイドが実際のオリエンテーションです。単一の統合されたワンコマンドのクイックスタートは存在しません。

これらはすべて OS を変更せずに対処できます。すでに安定したランタイム上のプロダクトポリッシュです。

## 関連情報

- `CLAUDE.md`(§ Constitution)— Product Think レンズの pass-line と、このページが依拠する bounding≠削減/legibility の区別
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — 7 つの feature family すべてで grounded された Product Think 行
- [observability.md](observability.md) — このページの「ライブな legibility」節と対をなす audit chip の dual-facet
- [リファレンス: cli/chat](../../reference/cli/chat.md)
- [リファレンス: config/budget](../../reference/config/budget.md)
