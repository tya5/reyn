---
title: Reyn vs OpenClaw / Hermes Agent — 全面比較調査
last_updated: 2026-06-19
status: snapshot
sources:
  - url: https://github.com/openclaw/openclaw
    accessed: 2026-06-19
  - url: https://github.com/NousResearch/hermes-agent
    accessed: 2026-06-19
  - url: https://github.com/tya5/reyn/issues/1837
    accessed: 2026-06-19
---

# Reyn vs OpenClaw / Hermes Agent — 全面比較調査

調査日: 2026-06-19 ・ 調査主体: broker maintainer (dogfood driver) ・
umbrella issue: tya5/reyn#1837

このページは **2026-06-19 時点のスナップショット**。13 領域で Reyn を OpenClaw /
Hermes Agent と比較した結論を、各領域に対応する GitHub issue (#1791〜#1835) への索引
として整理する。詳細・進捗は各 issue を参照。per-competitor の深掘りは姉妹ページ
[OpenClaw](openclaw.md) / [Hermes Agent](hermes-agent.md)。

調査対象:
- OpenClaw: `openclaw/openclaw`
- Hermes Agent: `NousResearch/hermes-agent`
- Reyn: `src/reyn/`

---

## 全体結論

**Reyn は「enforcement / 構造化 / 状態管理」の多くの領域で競合と同等以上。** 実在する穴は限定的で、「コンテンツ層防御」「外向き機能」「provider 層回復」「設計検討中の機能」に集中する。

設計思想の対比:
- **Reyn**: WAL を SSoT とした状態管理に全振り。DSL skill、scheme 抽象、多次元 safety、consistent-cut rewind。決定論・検証可能性・enforcement。
- **Hermes (NousResearch)**: モデル屋ゆえ「育てる」方向。skill 自動メンテ、trajectory 学習データ化、credential rotation、provider 固有制御。
- **OpenClaw**: マルチチャンネル製品ゆえ「サポート/運用」方向。診断バンドル、静的監査、Docker サンドボックス、channel 経由承認。

> **位置づけ.** 下記の「Reyn が優れている領域」(memory/RAG・sandbox・承認・budget・観測性・rewind・safety・scheme 抽象) は Reyn の設計上の強み。「実在する穴」(injection scan・gateway outbound・litellm Router・retry 工学・ACP・A2A 準拠) はロードマップ候補。

---

## 領域別 比較結論

### 1. システムプロンプト (issue #1791)
- Reyn の router_system_prompt は静的/動的分離・cache prefix 最適化が効いている。
- **欠けるもの**: Hermes の TASK_COMPLETION_GUIDANCE / PARALLEL_TOOL_CALL_GUIDANCE / memory quality guidance / model-family 別 guidance。非 Claude モデルの failure mode steering が弱い。
- 改善6件を #1791 に。

### 2. 接続層アーキテクチャ (#1808 closed, #1800, #1805, #1810, #1811, #1814)
- **3層整理**(#1808): 外部接続層(MCP/A2A/gateway)/ 内部トリガー層(cron/inject)/ ターン内介入層(hook)。lead-coder が3層モデル妥当と評価、docs 化済([interaction-layers](../../../concepts/architecture/interaction-layers.md))。
- **hook**(#1800): Reyn に agent lifecycle hook なし。Hermes の shell hook 方式(stdin/stdout JSON、Claude Code 互換 shape)を提案。inject_message は `runtime/triggers/` で別設計(lead-coder 決定)。
- **gateway outbound**(#1805 closed): Reyn の webhook は inbound のみ、outbound は MCP 委譲(これは綺麗な設計)。sample_slack が outbound MCP tool を提供していないのが穴。`plugins/` → `gateway/` リネーム(#1807 closed, lead-coder 承認)。
- **ACP**(#1810): Zed/JetBrains エディタ統合プロトコル(Agent Client Protocol)未対応。Hermes は `acp_adapter/` で完全実装。"ACP" = Agent Client Protocol (Zed) を指す(Agent Communication Protocol は A2A に吸収済み、deprecated)。
- **A2A**(#1811, #1814): メソッド11中8未実装、ステート名不一致、contextId 未対応。**最重要修正**: `a2a_routing.py` が全リクエストを共有セッション `a2a:a2a` に流す → per-contextId 分離が必要。Task layer は RunEntry 拡張 + A2A レイヤ変換で対応(コアほぼ不変)。A2A Task は RunEntry 相当で、Kanban とは別概念。

### 3. メモリ / RAG (#1820, #1821)
- **Reyn が優れている**: 1エントリ1ファイル + frontmatter(name/description/type)+ type 4種 × layer 2種。builtin RAG(recall + index_docs/index_events、memory も `.reyn/memory/*.md` をインデックス可)。OpenClaw は外部 QMD 委譲、Hermes は memory の semantic 検索なし。
- **欠けるもの**: ① 会話履歴の semantic 検索(chat EventLog はあるが index_events の chunker が skill run 単位のみ、会話 turn を拾わない → chat-turn chunker 追加で session_search 相当)② memory injection scan ③ 保存品質ガイダンス(Hermes MEMORY_GUIDANCE)。
- **compaction**(#1820): Reyn の ChatSummary は typed dataclass + section caps で構造化。Hermes/OpenClaw より上。欠けるのは再実行防止 preamble(pending を「今やること」と誤読させない)と tool output security strip。

### 4. セキュリティ / 権限 / サンドボックス (#1822)
- **実行層は Reyn が3システム最強**: Docker(DockerEnvironmentBackend)+ OS native syscall(Landlock/Seatbelt/Seccomp)両対応。permission の skill/path-scoped 承認。IV bus の承認チャネル配送 + 永続化。
- **唯一の穴 = コンテンツ層**: prompt injection scan(memory/tool result/context file が SP に毒注入される)と pre-exec command scan(homograph URL / pipe-to-shell)がない。Hermes は `threat_patterns.py`(all/context/strict scope)+ tirith。OpenClaw は external-content + 静的監査。
- #1820 #1821 のスキャン要望は #1822 の injection scan 機構1つに集約可能。

### 5. skill (#1823)
- **設計哲学が根本的に違う**: Reyn = DSL プログラム(phase graph + python preprocessor + JSON Schema、決定論的)。Hermes/OpenClaw = Markdown 指示書(LLM が読んで自分で実行)。再現性は Reyn 圧勝、学習コストは競合。
- **欠けるもの**: skill ライフサイクル自動化。Reyn は skill_builder/improver/importer/search のメタスキルを持つが「自動発火」がない。Hermes は skill_manage(create/patch) + background review の自動キュレーション + skills_guard(trust level + install スキャン)。

### 6. 委譲 / topology / capability (#1827)
- **Reyn の委譲安全機構は充実**: topology(network/team/pipeline の通信境界)+ max_agent_hops(再帰深度、default 3)+ chain_seconds(時間)+ permission.tool(静的能力)。
- 委譲モデル3者: Reyn = peer-to-peer 非同期(内部 delegate と外部 A2A が同じパターン)/ Hermes = subagent spawn 同期(子は fresh context + DELEGATE_BLOCKED_TOOLS)/ OpenClaw = subagent registry 非同期。
- **欠けるもの = 文脈依存の能力絞り込み**(#1827): permission.tool は静的上限で「この委譲では memory を触らせない」ができない。**目的はセキュリティ + 認知負荷削減の両立**(chat デフォルトは enumerate-all で全ツール提示されるため)。capability profile(カテゴリ scope + tool allow/deny)を topology/delegate/agent の3文脈に適用、実効 = 積集合。仕様未確定、案を並べて検討中。

### 7. モデルルーティング (#1829)
- litellm の扱いが3者で違う: Reyn = ライブラリ直接 import / Hermes = 完全自前 transport / OpenClaw = プロキシ委譲(TS で import 不可)。
- **なぜ競合は litellm を使わないか**: ① 言語(TS) ② provider 固有制御を握りたい(cache/rotation/reasoning 変換) ③ デバッグ可能性(litellm はブラックボックス)。
- **Reyn の穴**: `litellm.acompletion` を直接呼び `Router`(model_list + fallbacks + retry + cooldown)を使っていない。litellm の一番美味しい部分を取りこぼし。Router 導入で自前実装なしに fallback chain + credential rotation + cooldown を獲得できる。

### 8. コスト / 予算 (#1830)
- **Reyn が予算 enforcement で突出**: 多次元 hard cap(per-agent/daily/monthly/rate)+ fsync JSONL 台帳 + ask_on_exceed 対話延長。competitor は観測+警告止まり。
- コスト算出は litellm model_cost(2784モデル、cache/batch/priority 込み)で十分 — 当初「ズレる」と書いたのは過大評価で訂正。
- **欠けるもの**: Hermes の高額モデル事前確認(使う前に気づかせる UX)。provider 実コスト照会(OpenClaw)はニッチで低優先。

### 9. 観測性 / イベントログ (#1833)
- **Reyn がほぼ全勝**: WAL(StateLog、fsync/seq)+ ReplayEngine(決定論的再生)+ EVENT_AUDIT_REQUIREMENTS(監査完全性テスト)+ 外部 export(`eval.exporters[]`: file/langfuse/**OTLP**/IETF audit、Hermes より広い)。
- **唯一の穴**: OpenClaw の redaction 付き診断 support bundle(バグ報告用)。Reyn は trace/WAL/redaction の部品が全部あるので「束ねる出口」を足すだけ。

### 10. rewind / time-travel (穴なし、issue 不要)
- **Reyn 圧勝**: WAL seq への global consistent-cut(全 agent atomic 移動)+ branch/fork revival(死んだ分岐の復活)+ crash-mid-rewind recovery + workspace の content-addressed shadow-git(blob dedup、container mode 対応)。
- Hermes は shadow-git ファイルスナップショット(会話は直近 turn undo のみ)。OpenClaw は明示 rewind 薄い。
- 分散システム級の精緻さで競合の git snapshot を大きく上回る。**取り込む穴なし。**

### 11. safety / limit (穴なし、#1834 に確認事項のみ)
- **Reyn が体系性で圧勝**: 7次元(act-turns/phase-visits/router-cap/agent-hops/skill-calls/phase-sec/chain-sec)× 3モード(interactive/unattended/auto_extend)× 統一 handler(7サイト共有)。budget(財務)と loop(暴走)を分離。
- Hermes = iteration_budget(1次元)、OpenClaw = spawn_depth(1次元)。
- 確認事項: CodeAct の tool() ループが max_act_turns を過剰消費しないか(Hermes は execute_code を refund) → #1834 に追記。

### 12. ツールカタログ / dispatch (#1834)
- **Reyn の scheme 抽象が圧勝**: 4方式プラガブル(enumerate/universal/CodeAct/retrieval)× 層ごと選択(chat/step/phase)× P7(OS は scheme 固有概念ゼロ)。競合は各1〜2方式固定。
- 競合は互いを参照(Hermes が OpenClaw #84141 の catalog drift 失敗から「stateless 再構築」を学んだ)。
- **欠けるもの**: 閾値ベースの動的切替。chat=enumerate-all 固定(#1657 owner H1)なので、ツールが増えるとコンテキスト圧迫。Hermes の threshold gate(deferrable tools が context の 10% 超で discovery に切替)を取り込む。#1827(capability profile)と相乗。

### 13. リトライ / エラー処理 (#1835)
- 守備範囲が違う: Reyn = 意味的リトライ + 状態 resume(空応答/空stop/構造invalid/overflow + WAL plan-step resume + crash-resume) / Hermes = provider 障害回復(credential rotation + 賢い fallback 判定) / OpenClaw = retry 工学(jitter + Retry-After + 述語 runner)。
- **Reyn はアプリ/状態層で最強、provider 層が弱い**。
- **欠けるもの**: jitter(thundering herd 回避)と Retry-After 尊重。Reyn の `_backoff_s` は純粋指数 backoff のみ。credential rotation は #1829 でカバー。

---

## 調査で訂正した誤評価(教訓)

grep ヒットなし / 命名違いで「Reyn が弱い」と早合点し撤回した項目:

1. サンドボックス: 「OS native のみ」→ Docker も両対応(DockerEnvironmentBackend)
2. 承認チャネル配送 / 永続化: 「ない」→ IV bus(UserChannel + to_dict/from_dict)
3. 外部観測連携: 「口がない」→ `eval.exporters[]`(OTLP/Langfuse/IETF)実装済み
4. コスト精度(cache 割引): 「litellm でズレる」→ model_cost が cache/batch/priority 込み 2784 モデル網羅
5. 再帰委譲深度: 「ない」→ max_agent_hops
6. 委譲の通信境界: 「ない」→ topology(network/team/pipeline)
7. 構造化サマリー: 「ない」→ ChatSummary(typed dataclass + section caps)
8. semantic recall: 「ない」→ recall + index_docs(memory もインデックス可)

**教訓**: 「grep ヒットなし = 機能なし」ではない。命名・抽象が違うだけで実装済みが多い。実測・コード精読・canonical 確認を怠らない。

---

## 個別 issue 一覧

| # | タイトル | state |
|---|---|---|
| 1791 | SP improvements | open |
| 1800 | agent lifecycle hook system | open |
| 1805 | gateway outbound 未実装 | closed |
| 1807 | plugins → gateway リネーム | closed |
| 1808 | 3層整理 | closed |
| 1810 | ACP server 対応 | open |
| 1811 | A2A 準拠ギャップ | open |
| 1814 | A2A Task layer | open |
| 1820 | compaction 再実行防止 + security strip | open |
| 1821 | memory 強化(会話検索/injection/品質) | open |
| 1822 | コンテンツ層脅威スキャン | open |
| 1823 | skill ライフサイクル自動化 | open |
| 1827 | capability profile | open |
| 1829 | litellm Router | open |
| 1830 | コスト UX | open |
| 1833 | 診断 support bundle | open |
| 1834 | 閾値ベース動的 scheme 切替 | open |
| 1835 | retry jitter + Retry-After | open |
| 1837 | umbrella(本サマリー) | open |
