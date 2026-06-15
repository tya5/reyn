---
title: Reyn の競合劣位領域（将来強化候補）
last_updated: 2026-06-15
status: findings
audience: [maintainer]
---

# Reyn の競合劣位領域（将来強化候補）

2026-06 時点の競合調査（ClaudeCode / Codex / Cursor / Windsurf、Hermes /
OpenClaw、CrewAI / AutoGen / LangGraph）に基づく、Reyn が現状負けている領域の
findings 記録。owner 方針: 将来これらの強化を検討する。本ドキュメントは
findings の記録であり提案ではない — 断定で記すが、出典・段階は注記する。
なお §4（sandbox）は当初 gap と疑ったが調査の結果 core 技術は競争力ありと
判明した項目で、周辺機能の差分のみを gap として残す（経緯も記録として残す）。

> **出典・確度**: thenewstack / turingpost / Anthropic agent-autonomy / 各社
> 公式 docs（2026-06 時点、二次情報を含む）+ owner の実観測。競合側の数値・
> 主張は二次情報ベースで、時点依存。Reyn 側の記述はコードベース（feature-map /
> concepts）に照合済み。

## 1. コーディング / SWE 卓越性

ClaudeCode（Opus 4.7、最も深い harness）/ Codex（GPT-5.5）/ Cursor / Windsurf が
専用コーディング agent として圧倒。Reyn は SWE skill 最小化方針 + 汎用 agent 志向
ゆえ、専用 coding agent には劣位。Hermes ですら「SWE は Cursor / ClaudeCode に
劣る」と明言している。Reyn の SWE skill は OS 安定性の dogfood シグナル用途で
あって公表 SWE スコアの追求ではない（設計上の選択）。

## 2. 自己改善 / 学習ループ / 永続スキル自動生成

Hermes / OpenClaw は経験から self-generated skills を作り完了率を上げる（Nous の
internal 主張: 40% faster）。Reyn には `skill_improver`（eval-plan-apply の反復
改善）はあるが、**自律 trajectory 学習 → skill 自動生成ループ**は無い。改善は
明示的・人間ドリブンで、emergent な経験蓄積ループではない。

## 3. 基礎 tool-use 信頼性

owner 実観測（2026-06）: strong モデル（GPT-5.4）でも default の
`universal-category` scheme の tool 使用が下手で「使い物にならない」場面がある。
基礎的な tool-use の信頼性で現状負けている。**対応中** — tool-use scheme の
default 見直し + 代替 scheme（enumerate-all / retrieval / CodeAct）の整備が進行中
（[tool-use-schemes](../../../concepts/tools-integrations/tool-use-schemes.md) /
[codeact](../../../concepts/tools-integrations/codeact.md) 参照）。

## 4. Sandbox: core tech は競争力あり、周辺機能で差

**ここは Reyn の劣位ではない。** core sandbox 技術は競合と同等以上:

- ClaudeCode = Seatbelt（macOS）+ bubblewrap（Linux）+ network-proxy（opt-in）。
- Codex = Landlock + seccomp（default-on）。
- **Reyn = `SeatbeltBackend`（macOS SBPL deny-default）+ `LandlockBackend`
  （Linux 5.13+ Landlock LSM + seccomp-BPF stacking）** — macOS は ClaudeCode と
  同等、**Linux は ClaudeCode の bubblewrap より granular（syscall レベル）**。
  subprocess 実行中に syscall レベルで境界を強制し、宣言的 permission gating と
  独立・additive に重なる（stdio MCP server も Seatbelt 下で subprocess-sandboxed）。

つまり **core sandbox tech は gap ではない**。残る本当の差分は周辺機能のみ:

- **(a) network 隔離**: ClaudeCode の network-proxy 相当のネットワーク境界制御は
  未整備（`SandboxPolicy.network` の on/off はあるが proxy ベースの制御ではない）。
- **(b) cloud microVM 実行**: Claude Code for web 相当の cloud microVM サンドボックス
  実行環境は無い。
- **(c) default-on enforcement**: Codex は sandbox が default-on。Reyn の OS-sandbox
  は op/config 依存で、Codex のような全実行 default-on enforcement ではない。
- **(d) scope**: Reyn の OS-sandbox は `sandboxed_exec` op + stdio MCP subprocess に
  scope され、より広い surface（エージェントループ全体）への blanket 適用ではない。
- **(e) エコシステム成熟度**: backend caveat（`sandbox-exec` は upstream deprecated /
  macOS 26.3 で機能、Landlock は kernel 5.13+ 依存）。

reviewer-agent 自動承認 + per-action audit attribution（Codex）も無い（Reyn は
`chain_id` / `agent_id` の P6 audit 伝播は持つが reviewer-agent パターンではない）。

## 5. Computer use（browser / GUI 操作）

ClaudeCode は単一の agentic loop で computer use（browser / GUI 操作）が成熟。
Reyn は `web_search` / `web_fetch` のみで、GUI / browser 操作能力は無い。

## 6. 長時間自律の実績

ClaudeCode は 45 分超ターンの運用実績を持つ。Reyn の長時間自律の成熟度は未実証
（公表できる実績データが無い）。

## 7. multi-agent orchestration の成熟度

CrewAI / AutoGen / LangGraph は production の multi-agent orchestration で成熟。
Reyn は delegation レベル（topology-gated な `delegate_to_agent` + hop-depth cap +
`chain_id` 伝播）に留まり、organizational orchestration は未成熟。owner の
agent-cluster 構想は将来の差別化候補だが現状は未着手。

---

対になる Reyn の潜在優位（宣言的・可搬な OS / Skill 分離、制約付き valid 実行、
将来の共有 agent 組織）は [reyn-differentiators.md](reyn-differentiators.md) 参照。
