---
title: "Milestone: Reyn Flywheel — 自己改善と監査の両立"
last_updated: 2026-05-10
status: vision
---

# Milestone: Reyn Flywheel — 自己改善と監査の両立

> **「使うたびに賢くなる。ただし、何をどう学んだか全部説明できる。」**

---

## このマイルストンが意味すること

AI agent フレームワークの競合状況を調査した結果（2026-05）、
**自己改善と監査可能性を同時に出荷しているプロダクトは存在しない**ことが確認された。

```
Hermes GEPA:   自己改善 ✅ / 監査可能 ❌（Issue #17619 で EU AI 法違反が指摘）
LangSmith:     自己改善 ❌ / 監査可能 △（観測のみ）
その他:         どちらか一方のみ
```

このマイルストンが達成されると、Reyn は現時点で誰も解いていない問題を
production-grade で解決した最初のフレームワークになる。

---

## なぜ構造的に難しいか

自己改善と監査可能性は通常トレードオフになる:

```
自己改善しようとすると
  → LLM がシステムを書き換える
  → 何が変わったか追跡しにくくなる
  → 監査可能性が下がる

監査可能性を保とうとすると
  → 変更に承認ゲートを設ける
  → 自己改善が止まる / 遅くなる
```

Reyn がこれを両立できる理由は、**P6 イベントログ + Permission model**という
アーキテクチャレベルの設計が先にあるから。

- スキルの変更も `write_file` op → P6 に記録される（監査）
- `write_file` は Permission check を通る（制御）
- WAL で変更前後が追跡可能（ロールバック）

自己改善が「通常の OS 実行」として走るため、改善の証跡が自動的に残る。

---

## フライホイールの構造

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│   👤 使う → 📋 記録される → 🔍 インデックス化される  │
│        ↑                                    │        │
│        └──── ✨ 次の実行が改善される ◄──────┘        │
│                                                     │
└─────────────────────────────────────────────────────┘
```

フライホイールの特性:
- **最初から一定品質がある**ことが前提（品質が低いと逆回転する）
- **使い続けるほど加速する**（ルーティング精度・スキル品質の両方が向上）
- **全変化が P6 に記録される**（何をどう学んだかが説明可能）

---

## 構成 FP と依存関係

```
[基盤 — 達成済み]
  ADR-0033 RAG Phase 1 ✅
    embed / index_write / recall / index_query op
    index_docs スキル
    SqliteIndexBackend / SourceManifest

[Layer 1 — 比較的確実]
  FP-0009 Operational Intelligence
    index_events スキル（イベントログ → 知識ベース）
    ↓ が前提になる
  FP-0007 評価インフラ
    P6 export adapter / reyn eval CLI
  FP-0010 RAG ルーティング Phase 1
    スキルカタログの semantic pre-filter

[Layer 2 — モデル品質依存]
  FP-0006 スキル自己改善
    collect_traces → 失敗分析 → plan_improvements
    ← "失敗を正確に分析できるモデル強度" が必要
  FP-0010 RAG ルーティング Phase 2
    routing_decided 履歴から学習
    ← FP-0009 が育ってから
  FP-0008 SWE-bench
    コード修正・検証ループ
    ← frontier モデル相当が必要

[フライホイール完成条件]
  FP-0009 + FP-0006 + FP-0010 Phase 2 が揃ったとき
```

---

## 現状の正直な評価

| 項目 | 状態 | 備考 |
|---|---|---|
| 設計の正しさ | ✅ 確認済み | 競合調査・アーキテクチャ分析から |
| 基盤インフラ | ✅ 実装済み | P6 + RAG Phase 1 |
| FP-0009〜0010 Phase 1 | 🔧 設計済み・未実装 | 比較的達成可能 |
| FP-0006 自己改善品質 | ⚠️ 不確定 | モデル強度に依存 |
| FP-0008 SWE-bench | ⚠️ 不確定 | flash-lite では困難 |
| フライホイール e2e | 🔭 未検証 | 各パーツの品質が揃ってから |

**現時点では机上の設計**。基盤は本物だが、フライホイールとして回るかは
モデル品質・e2e 品質の積み重ねによる。

---

## 達成条件

以下が揃ったとき「フライホイール milestone 達成」と見なす:

1. `index_events` が P6 ログをインデックス化し `recall` で検索できる
2. `reyn eval compare` でスキルバージョン間の回帰比較が出力できる
3. RAG ルーティングが skill catalog から top-K を提示し、実際のルーティング精度が向上する
4. `skill_improver` が過去の失敗トレースを使って改善案を生成し、スコアが向上する
5. これら全ての変更が P6 に記録され、`routing_decided` / `skill_improved` / `skill_rolled_back` イベントで追跡できる

---

## このマイルストンが開く先

フライホイールが回り始めると、Reyn は「構築したもの」から「育つもの」に変わる。

**OSS ローンチ後のメッセージとして:**

> 「Reyn を使い続けるほど、あなたの組織のワークフローに最適化されていきます。
>  ただし、何をどう学んだかは全て記録されているので、いつでも確認・ロールバックできます。」

これは日本企業が求める「制御できる AI」と、
グローバル市場が求める「賢くなる AI」を同時に満たすポジショニングになる。

---

## 関連ドキュメント

- `docs/deep-dives/proposals/0006-skill-self-improvement.md`
- `docs/deep-dives/proposals/0007-evaluation-infrastructure.md`
- `docs/deep-dives/proposals/0008-swe-bench-integration.md`
- `docs/deep-dives/proposals/0009-operational-intelligence.md`
- `docs/deep-dives/proposals/0010-rag-routing.md`
- `docs/deep-dives/research/competitive/hermes-agent.md`
- `docs/deep-dives/research/landscape/reyn-strategic-priorities.md`
