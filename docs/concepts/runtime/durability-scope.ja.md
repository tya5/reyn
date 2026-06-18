---
type: concept
topic: architecture
audience: [human, agent]
---

# クラッシュ復旧の完全性と durability 境界

Reyn の約束は **Reyn が mediate する状態の crash-recovery を完全に保証する** こと
です。Reyn の mediation レイヤーを通る状態変更は、*実行が次へ進む前に* durable に
されるため、どの時点でクラッシュしても完全に復旧できます — 途中適用も、サイレントな
作業喪失もありません。本ページは、その約束が何を覆い、境界がどこにあるかを明示します。

## なぜ「全ファイルシステム」でなく境界なのか

プロセスが触れる *すべて* のバイトの復旧を保証することは達成不可能です: skill は
shell を呼べ、サブプロセスは権限のある場所へ書け、外部ツールは Reyn が見ないファイル
を変更できます。それら全部を復旧すると謳うのは、提供できないものを約束することです。

そこで Reyn は代わりに **明示的な境界** を引きます。Reyn の mediation を通る状態は
完全復旧可能、それ以外は best-effort。約束は願望でなく *厳密* です — **明示された境界
の内側では完全** は build 可能な保証であり、「全部復旧を試みます」はそうではありません。

## 境界の内側 — mediated、復旧保証あり

「内側」を定義する性質は **mediation** です: 状態変更が Reyn 自身の機構 — Control IR、
permission gate、event log — を通ることで、OS がその記録を持ち、replay / restore
できます。mediated な集合は 3 つの substrate から成ります:

- **Events / WAL** — すべての状態変更が write-ahead log に追記され、実行が続く前に
  append ごとに durable 化（同期 fsync）されます。復旧の背骨です。WAL と audit-event
  の区別は [Events](events.md) を参照。
- **Runtime snapshot** — 会話・実行状態が checkpoint 境界でスナップショットされ、
  point-in-time reconstruct のため WAL とペアになります。[タイムトラベル](time-travel.md)
  を参照。
- **Workspace artifacts** — Control IR `file.*` op を通じて書かれ、[permission model]
  (permission-model.md) で gate されるファイル群。すべての workspace 変更がその
  permission-checked・event-logged チャネルを通るため、workspace は復旧可能な境界の
  一部です。[Workspace](workspace.md) を参照。

> **境界の extent。** どの write path が mediated（保証）で、どれが mediation を
> bypass する（best-effort）かの正確な inventory は、稼働中システムの
> filesystem-mediation flow-trace から導かれます。本ページは境界の *原則* を示し、
> path 単位の extent はその trace から map してここに同期します。

## 境界の外側 — best-effort、保証なし

Reyn の mediation を通らないファイルシステムアクセスは復旧保証の外です。カテゴリ別:

- ツールやサブプロセスが Control IR op を通さずに行う直接書き込み。
- workspace が参照するが所有しない外部ファイル。
- permission-gated・event-logged チャネルを bypass するもの全般。

これらはクラッシュを生き延びるかもしれませんが、Reyn は **完全性を約束しません** —
定義上、OS が replay する mediated な記録を持たないからです。復旧されねばならない作業は
mediated チャネルを通すべきです。

## 現状: runtime / workspace の durability 非対称

境界の *scope* とは別に、その *durability* が今どこにあるかを正直に述べます:

- **Runtime substrate（WAL + snapshots）** は **電源断 durable** — fsync-per-append
  契約により、OS クラッシュや電源断でも commit 済みのものは失われません。
- **Workspace file substrate** は **まだ fsync-ordered でない** — ハードな電源断では
  workspace ファイルがペアであるべき runtime 状態と乖離しうります。

この非対称は確定した設計判断ではなく **in-progress** です。方向性は **workspace の
durability を境界内で対称化する** こと — WAL を鏡映する durability + ordering barrier
の下に workspace を置き、内側集合全体を（runtime 半分だけでなく）電源断 durable に
することです。それが入るまで、完全性保証は clean なプロセスクラッシュに対して成立し、
workspace ファイルの電源断エッジが塞がれつつある gap です。境界の原則は変わりません —
変わるのは、その durability が現状どこまで届くかだけです。

## 関連

- [Events](events.md) — WAL と fsync-per-append durability 契約
- [タイムトラベル](time-travel.md) — WAL + runtime snapshot 上の reconstruct
- [Workspace](workspace.md) — mediated artifact store
- [Permission model](permission-model.md) — write を mediate する gate
- [クラッシュ復旧 / skill resume](../skills/skill-resume.md) — 復旧機構
