---
type: concept
topic: architecture
audience: [human, agent]
---

# System Design

agent システムのマクロ構造: 制御フロー、状態、責任をレイヤーを横断してどのように分散させるか、そしてランタイムが LLM が何をしても強制する不変条件は何か。

## Reyn の実装方法

### レイヤー分割: LLM が決定し、OS が実行し、feature が自身のドメインを所有する

Reyn の現行の形には、辿るべき固定の phase グラフはもうありません。代わりに、3 つの責任が構造的に分離されています:

| レイヤー | 所有するもの | 知っていること |
|-------|------|-------------|
| **LLM** | 次に何をするか、どの順番で、どんな引数で | 現在のターンのコンテキストフレームに含まれるものだけ — それ以外は何も知らない |
| **OS** | op dispatch、スキーマ検証、permission gate、audit-event ログ、workspace | workflow/skill/pipeline 固有の文字列リテラルは一切知らない(今も有効な P7 不変条件) |
| **Feature(skill / pipeline / agent)** | 自身の指示、自身の artifact、自身のスコープ | 与えられたもの以外は何も知らない — ある skill は他の skill の状態に踏み込めない |

LLM がどのサーフェス(chat router、A2A、MCP)経由で話していても、2 つの不変条件が成り立ちます:

1. **すべての副作用は typed で schema-validated な Control IR op であり、LLM が手作りする自由形式の文字列では決してない。** OS は不正な形式の op を実行前に拒否します。LLM の出力から op-schema 検証を経ずに直接副作用に至る経路は存在しません。
2. **すべての op は、どの tool-use scheme が LLM に提示していようと(ネイティブ function-calling、universal action catalog wrapper、CodeAct、…)、同じ exclude → permission → dispatch ゲートを通ります。** 提示レイヤーは差し替え可能ですが、その下にあるゲートは差し替え不可能です。

### 構造的な有界性が今も存在する場所: Pipeline

旧 phase-graph engine の安全性の論拠は有限状態機械でした: LLM は閉じた候補集合からエッジを選ぶことしかできず、run 全体が証明可能に有界でした。その特定の機構は無くなりましたが、同じ*種類*の保証 — LLM が逃れられない、閉じた non-Turing-complete な control-plane — は、それを望むケースに対して今日も存在します: **Pipeline** です。pipeline は決定論的な DSL であり、その合成プリミティブは構造的に閉じています(ネストした `launch` なし、任意の再帰なし)。安全性とクラッシュリカバリは、無制限の実行グラフの上に重ねられたランタイムポリシーからではなく、DSL の形そのものから来ます。

Chat(router loop)は意図的にこの同じ厳密な有界性を再導入**しません** — その安全性の論拠は異なります: typed-op 検証 + permission gate + force-close 付き bounded-loop + audit-event/WAL trail であり、LLM が踏み外せない閉じたグラフではありません。あるタスクに Pipeline と chat-router オーケストレーションのどちらを選ぶかは、この 2 つの安全性の論拠のどちらを望むかを選ぶことです。

### 現行の具体的な機構

- **Chat session / router loop** — `RouterLoop` が LLM が選んだ各アクション(skill run、agent delegation、pipeline launch、MCP call、memory op、…)を、同じ permission-gated な dispatch パス経由で実行します。
- **Skill** — 段階的開示の instructions(L1 system-prompt メニュー → L2 on-demand フル読み込み → L3 バンドル済み asset 読み込み)であり、OS が実行するプログラムではありません。モデルが読むかどうかを選びます。
- **Environment backend 抽象化** — `EnvironmentBackend` が repo-FS の read/write/exec が *どこで* 行われるか(host か container か)を OS + permission レイヤーから完全に抽象化するため、実行場所によって governance レイヤーが変わることはありません。
- **Workspace(P5)がシングルソースオブトゥルース** — agent が生成するすべての artifact は workspace チャンネルを通過します。LLM のコンテキストウィンドウにしか存在しない load-bearing なものはありません。
- **P6 audit-event ログ** — OS が引き起こすすべての状態変化は audit-event を発行し、LLM が何をしたかに関係なく、すべての run に完全でリプレイ可能な記録を残します。

## まだ薄い部分

phase-graph の厳密な有限状態機械を手放したことによるトレードオフは、見かけだけのものではなく本物です: chat-router オーケストレーションはもはや、ある run が終了する、または固定された経路の集合にとどまるという*構造的*保証を与えません — その保証は今や、閉じたグラフの形(LLM が文字通り違反できない構造的性質)ではなく、force-close 付き bounded-loop(ランタイムポリシー)に存在します。Pipeline は、タスクが構造的保証を必要とするときに戻るためのエスケープハッチであり、chat オーケストレーションはより open-ended なデフォルトです。このトレードが見合うかどうかはタスクごとの判断であり、決着済みの問題ではありません — 現行の bounded-loop 機構がどう働くかは [reliability-engineering.md](reliability-engineering.md) を参照してください。

## 関連情報

- `CLAUDE.md`(§ Constitution)— System Design レンズの pass-line、canonical
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — 7 つの feature family すべてで grounded された System Design 行
- [tool-contract-design.md](tool-contract-design.md) — LLM がどのように選択を表現するか(このページの不変条件 #1 が依存する typed op contract)
- [reliability-engineering.md](reliability-engineering.md) — LLM が間違えたときに何が起こるか、force-close 付き bounded-loop 機構がどう働くか
- [`docs/concepts/runtime/pipelines.md`](../runtime/pipelines.md) — Pipeline の構造的な閉じ方の全体
