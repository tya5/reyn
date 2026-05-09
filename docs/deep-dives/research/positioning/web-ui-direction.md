# Reyn Web UI direction — `reyn chat` (local + embedded) + `reyn serve` (= remote 接続は `reyn chat --host`)

**Status**: Design direction (2026-05-09 update 3) — UX positioning。 architectural commitment ではないため ADR ではなく positioning doc として記録。 **実現性検討は未着手**。
**Track**: Web UI / CLI 統合 UX

> Update 履歴: (1) 元 ADR-0028 「同一 process embedded」、 (2) 一度 `reyn serve / reyn client` の 2 軸案に shift、 (3) update 3 で **業界慣行 (Ollama / Docker / vLLM) に合わせ `reyn client` subcommand を廃止**、 「remote 接続も `reyn chat --host <addr>` or `REYN_HOST=...` env で同 chat command 上に統合」 に整理。 commands は **`reyn chat` (local default + embedded Web UI、 `--host` で remote)** と **`reyn serve` (= explicit long-running server)** の 2 つ。 feasibility 未検討、 ADR 化は前提整備が complete してから。

## 業界慣行との整合 (= update 3 の motivation)

| Tool | Server start | Client (local default) | Remote target |
|---|---|---|---|
| Ollama | `ollama serve` | `ollama run` (auto-start local) | `OLLAMA_HOST=…` |
| Docker | `dockerd` | `docker …` | `DOCKER_HOST=…` |
| kubectl | (cluster側) | `kubectl …` | `--context` / `KUBECONFIG=…` |
| vLLM | `vllm serve` | (= no built-in CLI client) | n/a |
| LangGraph | `langgraph up` | (= web UI 経由) | n/a |
| **Reyn (current direction)** | **`reyn serve`** | **`reyn chat`** (local + embedded Web UI) | **`reyn chat --host <addr>` or `REYN_HOST=…`** |

`<tool> serve` は modern AI tooling の dominant pattern (Ollama / vLLM / Tabby / LangGraph)、 remote target を env var or `--host` flag で切替えるのは Docker / Ollama / kubectl で確立した pattern。 `reyn client` という subcommand 名は industry に存在しないため廃止、 `reyn chat` に flag 統合する。

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

### 2 commands + transport flag

```
reyn chat                       # default: local in-process AgentWorker
                                # + embedded Web UI server (session 生存期間 bind)
                                # サーバ概念は意識不要、 URL は表示のみ
                                # → 旧 ADR-0028 embedded thesis をこちらで実現

reyn chat --host <addr>         # 同 TUI codebase が remote server に接続
   (or REYN_HOST=<addr> reyn chat)
                                # transport だけ remote 切替
                                # 表示・入力 UX は local mode と同一
                                # embedded Web UI server は起動しない (= remote が serve 担当)

reyn serve [--bind 0.0.0.0:8765]  # explicit server daemon
                                # 明示的に長生き、 user-managed
                                # multi-user / multi-device 用
```

`reyn chat` (local) と `reyn chat --host` (remote) は **同一 TUI codebase**、 違いは AgentWorker が local in-process か remote server かだけ (= 抽象 layer 1 枚で切り替え)。

### ライフサイクル原則

```
reyn chat (local)        → process 終了で TUI + embedded Web UI 両方消える
                           (= session lifecycle = 全 server lifecycle、 孤児なし)
reyn chat --host         → server に依存、 server 落ちたら disconnect 表示
                           local 側に embedded Web UI server は立てない
reyn serve               → user が明示的に start / stop (= 自覚あり)
```

「local で完結したい light user」 (= `reyn chat` で TUI + browser から URL 開けば Web UI も使える) と「multi-user / multi-device で運用したい power user」 (= `reyn serve` + `reyn chat --host`) が **orthogonal な選択肢**として分離されている。 embedded thesis (= TUI lifecycle = Server lifecycle) は `reyn chat` 内で温存される。

### Multi-user / multi-device の自然な path

`reyn serve` + `reyn chat --host` model を別軸として置くことで、 embedded だけでは塞がっていた pathway が開く:

- 1 server に複数 `reyn chat --host` 接続 (= teams)
- 同じ session に web browser + TUI 接続が同居
- mobile で server 状況を確認 (= browser 経由)
- 中央 server で audit / quota / compliance 集中管理

`reyn chat` の embedded だけでは「自分の TUI を立ち上げないと server が動かない」 ので multi-user / multi-device pathway が塞がる。 `reyn serve` がその限界を解消する別系統。

---

## 3. 内部アーキテクチャ (= 概念レベル、 実現性検討前)

### Same-codebase, different transport

```
TUI codebase (= chat/tui/)
  ├── AgentWorker reference (= local or remote)
  └── 描画・入力 layer

reyn chat (local default + embedded Web UI):
  同一 process:
    ├── TUI ──→ AgentWorker (direct coroutine)
    ├── embedded Web UI server ──→ AgentWorker (direct coroutine + SSE)
    └── AgentWorker (asyncio, 共有インスタンス)
         └── Workspace (P5 SSoT)

reyn serve:
  独立 process:
    ├── AgentWorker (asyncio)
    ├── Workspace (P5 SSoT — server side)
    └── HTTP / SSE / WebSocket endpoints

reyn chat --host <addr> (= remote 接続):
  独立 process (= remote machine も可):
    └── TUI ──→ HTTP / SSE / WebSocket ──→ reyn serve の AgentWorker
    (= embedded Web UI server は起動しない)
```

抽象 layer の design choice (= Protocol 化 / Adapter pattern / RPC contract) は feasibility 検討の主題。 `reyn chat` の direct-coroutine path と `reyn chat --host` の remote path が同一 TUI interface に投影できることが最大の前提。

---

## 4. 競合との比較

| フレームワーク | 構造 | 孤児リスク | client UX |
|---|---|---|---|
| LangGraph Studio | 別サーバー (HTTP) | あり | Web only |
| AutoGen Studio | 別サーバー (HTTP) | あり | Web only |
| CrewAI+ | クラウド | なし | Web only |
| Claude Code | local subprocess | なし | TUI only (= no remote) |
| **Reyn (current direction)** | **`reyn chat` (local + embedded Web UI) / `reyn serve` (explicit)** | **chat は session bind で孤児なし、 serve は user-managed** | **`reyn chat` (local default) と `reyn chat --host` (remote) は同 codebase + chat 単独で Web UI も同梱** |

差別化 point:
- 「ライト user は `reyn chat` 1 コマンドで TUI + Web UI 両方手に入る」 (= 旧 ADR-0028 thesis)
- 「power user は `reyn serve` + `reyn chat --host` で multi-user / multi-device」 (= LangGraph Studio が unique に持っていた領域)
- 「TUI codebase は 1 つ、 transport で local / remote 切替」 (= 機能 drift なし、 Ollama / Docker 流の同 binary 設計)

---

## 5. Tradeoffs

### ✓ 得られるもの

- `reyn chat` で light user UX 維持 (= サーバ概念なし、 session bind で孤児なし)
- `reyn chat` 単独で TUI + Web UI 両方使える (= 旧 ADR-0028 embedded thesis を温存)
- `reyn serve` + `reyn chat --host` で multi-user / multi-device path
- TUI codebase は 1 つ、 local / remote の AgentWorker abstraction 切り替えのみ
- 「light user で始めて → 必要になったら serve に scale up」 の漸進 path が明確
- 業界慣行 (= Ollama / Docker / vLLM / kubectl) と整合した命名で onboarding 摩擦が低い

### △ トレードオフ

- **複雑度増加**: AgentWorker abstraction layer (= local / remote 切替) + embedded Web UI server の同梱、 TUI codebase に「どちらの mode で動いているか」 の awareness が一部漏れる risk
- **`reyn chat` 内の embedded Web UI と AgentWorker concurrency**: 同一 process で TUI input / browser input が並走、 単一 ChatSession に 2 input source が暗黙前提 (= 旧 ADR-0028 でも未解決だった項目、 `reyn serve` の concurrency と統合的に設計したい)
- **`reyn serve` の auth / authorization 設計が必須**: `reyn chat --host` で remote 接続する以上、 token / TLS / origin check 等は ADR 化前提
- **`reyn chat` (local) と `reyn chat --host` の UX 差分管理**: code は同じでも remote latency / disconnect / retry の UX rule は別途必要
- **「同 command が local / remote で挙動が違う」 の発見性**: Ollama / Docker は env var が標準なので user は知っている前提だが、 Reyn の new user には `--host` flag 存在を doc / `reyn chat --help` で明示する必要

---

## 6. 実現性検討の前 TODO

実装着手前に評価が必要 (= 未着手):

1. **AgentWorker abstraction の design feasibility**: local in-process と remote RPC を 1 つの interface で抽象化できるか。 既存 ChatSession / RouterLoop / dispatch_tool が前提とする invariants (= asyncio event loop / direct method call) を守ったまま remote 化が可能か。
2. **`reyn chat` 内 embedded Web UI server の port / lifecycle**: session 開始時に port allocate (= ephemeral or 8765 fixed?)、 session 終了時に確実に release。 同 host で多重 `reyn chat` が走る場合の port collision policy。
3. **`reyn chat` 内 embedded vs `reyn serve` の同 codebase 化**: embedded Web UI server と `reyn serve` の HTTP / SSE endpoint は同じ code path で良いか (= AgentWorker location だけ違う)、 別実装か。 同 codebase 化が望ましいが session bind なら ephemeral 設定 + auth skip 等の差分管理が必要。
4. **Workspace (P5) の location 設計**: `reyn serve` 側に Workspace、 client 側は viewer のみ? それとも client にも cache layer? events stream で artifact 同期する? P5 SSoT 不変条件をどう保つか。
5. **Events (P6) の transport 設計**: SSE / WebSocket / gRPC streaming のどれが Reyn の event shape に fit するか。 backpressure / retry / out-of-order handling の rule。
6. **AgentWorker concurrency**: `reyn chat` 内 embedded で TUI + browser から並列 input、 もしくは `reyn serve` で複数 client 接続時、 同一 ChatSession に 2+ input source の concurrency model (= 直列化 / per-client session / locking)。
7. **Auth / Authorization**: `reyn serve` の network exposure に対する token / TLS / per-client permission baseline。 `reyn chat` 内 embedded は localhost-only + token 自動発行で auth skip 可能か。 enterprise 視点で必須。
8. **`reyn chat --host` の disconnect / retry UX**: server が一時的に落ちた / network 切れた時の TUI 表示・入力受付・state 復帰。
9. **plan-mode async (ADR-0022/0023) との整合**: server 側で plan 走行中に `reyn chat --host` disconnect → reconnect、 plan は detach されたまま完走、 reconnect で resume? semantics。 `reyn chat` (local) でも TUI 閉じる前に browser tab で plan 監視中なら? の UX。
10. **`--host` flag vs `REYN_HOST` env var 優先順位**: 両者併用時の precedence (= flag が env を override) は Docker / kubectl 慣例に倣う。 `reyn.yaml` 設定との関係も明確化必要。
11. **prototype 範囲**: feasibility study の output として、 何を minimum demo として作るか (= echo agent + 1 remote `reyn chat --host` / events stream のみ / `reyn chat` embedded の URL 表示まで / etc.)。

これらが解決した時点で **`Web UI 機能スコープ ADR` (= Web frontend は何を見せる / TUI は何を留める)** を defining する ADR を作り、 そこから順次 sub-ADR を切る pathway。

---

## 7. 関連

- ADR-0027: AuditSeal 分離 — `docs/deep-dives/decisions/0027-audit-seal-separation.md` (= server 側で seal、 `reyn chat --host` 経由で監査ログ閲覧)
- P5: Workspace SSoT (= server / client 間の location 設計の基盤)
- P6: Events (= server → client への transport の基盤)
- 旧案 (= embedded same-process only): この doc の git history (= rename 前 `embedded-web-server-ux.md`) に保存。 「TUI lifecycle = Web Server lifecycle」 を embedded only で組み立てた direction。 update 2 で「embedded は `reyn chat` 内に温存 + `reyn serve / reyn client` を別軸で追加」 に再整理。 update 3 で **業界慣行 (Ollama / Docker) に合わせ `reyn client` を廃止 → `reyn chat --host` に統合**。 embedded thesis 自体は捨てていない、 multi-user / multi-device path を「同 chat command + flag」 で開いた。
