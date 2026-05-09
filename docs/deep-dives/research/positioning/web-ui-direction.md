# Reyn Web UI direction — `reyn serve` + `reyn client` model

**Status**: Design direction (2026-05-09 update) — UX positioning。 architectural commitment ではないため ADR ではなく positioning doc として記録。 **実現性検討は未着手**。
**Track**: Web UI / CLI 統合 UX

> 元 ADR-0028 の「同一 process embedded」 案から、 **`reyn serve` (= 明示的 server daemon) + `reyn client` (= 同 TUI codebase が remote 接続)** という client-server model に direction shift した。 同じ TUI codebase が local mode (`reyn chat`) と remote mode (`reyn client`) 両対応するのが core idea。 feasibility 未検討、 ADR 化は前提整備が complete してから。

---

## 1. Context

### 解決したい問題

(1) **「サーバを起動したが、クリーンアップしていなくて生き続けている」**
開発者ツールの典型的な失敗パターン (= LangGraph Studio / AutoGen Studio の「別サーバー HTTP」 パターン)。

(2) **「Web UI と TUI で全く別の codebase が並走する」**
Web UI を作ると TUI と機能 drift が発生し、 メンテコストが二重になる。

### 想定するユーザー像

- **ライトユーザー**: `reyn chat` だけ知っていれば local で完結する
- **チームユーザー**: 自宅 PC で `reyn serve` 立てて、 別マシン / mobile から `reyn client` で接続したい
- **enterprise**: 中央 server (= compliance / audit / quota 集中管理) に複数開発者が `reyn client` で接続

---

## 2. Direction (現在の vision)

### 3 mode 構成

```
reyn chat              # local in-process (= 現状の動作)
                       # サーバ概念なし、 light user 向け

reyn serve             # explicit server daemon
                       # 明示的に長生き、 user-managed

reyn client <addr>     # 同じ TUI codebase が remote server に接続
                       # 表示・入力 UX は reyn chat と同一
```

`reyn chat` と `reyn client` は **同一 TUI codebase**、 違いは AgentWorker が local in-process か remote server かだけ (= 抽象 layer 1 枚で切り替え)。

### ライフサイクル原則

```
reyn chat   → process 終了で全部消える (= 孤児なし、 light user OK)
reyn serve  → user が明示的に start / stop (= 自覚あり)
reyn client → server に依存、 server 落ちたら disconnect 表示
```

「 server を立てる」 と「 server に繋ぐ」 が orthogonal な action として分離。 これは embedded model の central thesis (= TUI lifecycle = Server lifecycle) を明示的に放棄する。

### Multi-user / multi-device の自然な path

`reyn serve` + `reyn client` model は、 設計を意識せずとも以下が成立する:

- 1 server に複数 client 接続 (= teams)
- 同じ session に web browser + TUI client が同居
- mobile で server 状況を確認

embedded model だと「自分の TUI を立ち上げないと server が動かない」 ので multi-user / multi-device pathway が塞がっていた。

---

## 3. 内部アーキテクチャ (= 概念レベル、 実現性検討前)

### Same-codebase, different transport

```
TUI codebase (= chat/tui/)
  ├── AgentWorker reference
  └── 描画・入力 layer

local mode (= reyn chat):
  AgentWorker ←── direct coroutine ── TUI

remote mode (= reyn client):
  AgentWorker ←── HTTP / SSE / WebSocket ── reyn client TUI
                  (= reyn serve が host)
```

抽象 layer の design choice (= Protocol 化 / Adapter pattern / RPC contract) は feasibility 検討の主題。

### State / events stream

```
reyn serve:
  ├── AgentWorker (asyncio)
  ├── Workspace (P5 SSoT — server side)
  └── Events stream (P6) → SSE / WebSocket で client に push

reyn client:
  ├── server から events subscribe
  └── server に user input を送信
```

---

## 4. 競合との比較

| フレームワーク | 構造 | 孤児リスク | client UX |
|---|---|---|---|
| LangGraph Studio | 別サーバー (HTTP) | あり | Web only |
| AutoGen Studio | 別サーバー (HTTP) | あり | Web only |
| CrewAI+ | クラウド | なし | Web only |
| Claude Code | local subprocess | なし | TUI only (= no remote) |
| **Reyn (current direction)** | **mode 切替 (chat / serve / client)** | **chat は孤児なし、 serve は user-managed** | **TUI codebase 共通 (local + remote 両対応)** |

差別化 point: **「ライト user は local で完結、 power user は server-client で multi-device」 が同じ TUI codebase で連続的に提供できる**。

---

## 5. Tradeoffs

### ✓ 得られるもの

- `reyn chat` で light user UX 維持 (= サーバ概念なし、 孤児なし)
- `reyn serve / client` で multi-user / multi-device path
- TUI codebase は 1 つ、 local / remote の AgentWorker abstraction 切り替えのみ
- Web UI が必要なら `reyn serve` + browser から接続する web frontend を別途作れる (= 必須ではない)

### △ トレードオフ

- **複雑度増加**: AgentWorker abstraction layer (= local / remote 切替) が必要、 TUI codebase に「どちらの mode で動いているか」 の awareness が一部漏れる risk
- **embedded model の simplicity を失う**: 旧案は「同一 process で全部完結」 が elegant だった
- **server の auth / authorization 設計が必須**: `reyn client` で remote 接続する以上、 token / TLS / origin check 等は ADR 化前提
- **`reyn chat` と `reyn client` の UX 差分管理**: code は同じでも remote latency / disconnect / retry の UX rule は別途必要

---

## 6. 実現性検討の前 TODO

実装着手前に評価が必要 (= 未着手):

1. **AgentWorker abstraction の design feasibility**: local in-process と remote RPC を 1 つの interface で抽象化できるか。 既存 ChatSession / RouterLoop / dispatch_tool が前提とする invariants (= asyncio event loop / direct method call) を守ったまま remote 化が可能か。
2. **Workspace (P5) の location 設計**: server 側に Workspace、 client 側は viewer のみ? それとも client にも cache layer? events stream で artifact 同期する? P5 SSoT 不変条件をどう保つか。
3. **Events (P6) の transport 設計**: SSE / WebSocket / gRPC streaming のどれが Reyn の event shape に fit するか。 backpressure / retry / out-of-order handling の rule。
4. **AgentWorker concurrency**: 1 server に複数 client 接続の場合、 同一 ChatSession に 2+ input source の concurrency model (= 直列化 / per-client session / locking)。
5. **Auth / Authorization**: `reyn serve` の network exposure に対する token / TLS / per-client permission 等の baseline。 enterprise 視点で必須。
6. **`reyn client` の disconnect / retry UX**: server が一時的に落ちた / network 切れた時の TUI 表示・入力受付・state 復帰。
7. **plan-mode async (ADR-0022/0023) との整合**: server 側で plan 走行中に client が disconnect → reconnect、 plan は detach されたまま完走、 reconnect で resume? semantics 設計。
8. **prototype 範囲**: feasibility study の output として、 何を minimum demo として作るか (= echo agent + 1 client / events stream のみ / etc.)。

これらが解決した時点で **`Web UI 機能スコープ ADR` (= Web frontend は何を見せる / TUI は何を留める)** を defining する ADR を作り、 そこから順次 sub-ADR を切る pathway。

---

## 7. 関連

- ADR-0027: AuditSeal 分離 — `docs/deep-dives/decisions/0027-audit-seal-separation.md` (= server 側で seal、 client から監査ログ閲覧)
- P5: Workspace SSoT (= server / client 間の location 設計の基盤)
- P6: Events (= server → client への transport の基盤)
- 旧案 (= embedded same-process model): この doc の git history (= rename 前 `embedded-web-server-ux.md`) に保存。 「TUI lifecycle = Web Server lifecycle」 を中心に組み立てた embedded direction。 multi-user / multi-device path が塞がる弱点で direction shift。
