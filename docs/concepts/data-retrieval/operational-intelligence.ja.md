---
type: concept
topic: operational-intelligence
audience: [human, agent]
---

# Operational Intelligence

**この機能は現在存在しません。** 本ページがかつて説明していたワークフロー — Reyn 自身の P6 audit-event ログ(`.reyn/events/*.jsonl`)を safe-mode `index_update()` ステップ経由で in-core RAG store に index し、`semantic_search` で実行履歴をセマンティックにクエリする — には、もはや経路が一切残っていません。FP-0066 P1b で agent 向けの layer-1 RAG ツール4件(`semantic_search` / `index_update` / `drop_source` / `list_rag_sources`)が retire され、FP-0066 P1c で残っていた safe-mode `index_update()` Python エントリーポイントと CLI `reyn source` コマンド群も retire されました。in-core store に対して operator/agent 向けに追加・削除・検索する手段は、もはや一切ありません — 完全な retire 経緯は [コンセプト: RAG](rag.ja.md) を参照してください。

これは見落としではなく意図的な撤去です — 「無い」だけを見た将来の読者が「忘れられたのか、決められたのか」を区別できるよう、ここに記録しています。撤去されたのは agent 向けツール層だけです: `semantic_search`/`index_update`/`index_drop` の op kind 自体は意図して残されており、後続の FP-0066 §8 ingest phase が基盤として使います — 今日 P6 イベントログを index する手段としては使えませんが、削除された語彙でもありません(#5495)。

**現在の入口**(参考まで): `search_actions`(tool/mcp/pipeline カタログ検索)は現在稼働中、`search_knowledge`(skill/memory/repo 取得)も FP-0066 P3c 以降 稼働しています——どちらも P6 イベントログや任意のコーパスを index するものではありません。agent 向けのドキュメント検索については、現在の経路は FP-0063 user RAG plugin(外部 vector store、ドキュメントのみ)です — [Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) を参照してください。event ログのようなものをこの plugin 経由で index するワークフローは、構築も実測もされていません — もし着手する価値があるなら、本ページへの主張としてではなく、独立した設計・issue として立てるべきです。

## 関連情報

- [コンセプト: RAG](rag.ja.md) — 完全な retire 経緯と残っているもの
- [コンセプト: Events](../runtime/events.ja.md) — P6 イベントログの構造と現行のイベント分類
- [FP-0009: Operational Intelligence](../../deep-dives/proposals/0009-operational-intelligence.ja.md) — 元の設計根拠(historical。ここで提案された仕組みは後に retire 済み)
