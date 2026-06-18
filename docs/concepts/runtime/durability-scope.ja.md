---
type: concept
topic: architecture
audience: [human, agent]
---

# クラッシュ復旧の完全性と durability 境界

Reyn の約束は **workspace tree 内に land するファイル内容**（および runtime
substrate = event/WAL log と runtime snapshot）の crash-recovery を完全に保証する
ことです。どの時点でクラッシュしても、その状態は完全に復旧されます — 途中適用も、
サイレントな作業喪失もありません。本ページは、約束が何を覆い、境界がどこにあるかを
明示します。

## なぜ「全ファイルシステム」でなく境界なのか

プロセスが触れる *すべて* のバイトの復旧を保証することは達成不可能です: skill は
shell を呼べ、サブプロセスは権限のある場所へ書け、外部ツールは Reyn が見ないファイル
を変更できます。それら全部を復旧すると謳うのは、提供できないものを約束することです。

そこで Reyn は代わりに **明示的な境界** を引きます。境界は **workspace-tree
membership**（書かれたファイルが workspace tree の中にあるか）です。tree 内の状態は
復旧可能、それ以外は best-effort。約束は願望でなく *厳密* です — **明示された境界の
内側では完全** は build 可能な保証であり、「全部復旧を試みます」はそうではありません。

## 2 つの層: tracking と content-recovery

Reyn が行う 2 つの別物を分けると理解しやすくなります。スコープが異なるためです:

- **L1 — per-mutation tracking。** Reyn は *書き込み毎に* event を出すか? これは
  **Control IR file op のみ**（`file.write` / `file.edit` / `file.delete` →
  `workspace_updated` audit event）を覆います。L1 は **audit 専用** — 変更が起きた
  ことを記録するだけで、復旧機構 *ではありません*。
- **L2 — file-content recovery。** クラッシュ後に実バイトを復元できるか? これは
  各 generation cut で **shadow-git が捕捉する workspace tree 全体**（work-tree 全体への
  `git add -A` を commit し `reyn-gen-<seq>` で tag）です。L2 は **書き方に依らず**、
  tree 内にある限りファイルを捕捉します。

**復旧境界は L2 — tree membership です。** L1（per-mutation tracking）は厳密により
狭い（Control IR のみ）。あるファイルは個別 track（L1）されずとも完全に復旧可能（L2）
でありえます。

## 境界の内側 — content-recoverable

結果が workspace tree 内に land し、L2 が捕捉する書き込み:

- **Control IR file op** — permission-gated、L1-tracked、**かつ** L2-captured。
  完全に覆われる: 変更毎に track され、内容として復旧可能。
- **workspace tree 内への `sandboxed_exec` 書き込み** — `cwd` を workspace base dir
  に置き相対パスで書けば、出力は tree 内に land します。**L2 recovered だが L1
  untracked**（per-write event は無い — 復旧は audit 記録でなく tree capture による）。
- **container backend の work-tree 内書き込み** — L2 で捕捉（runner が work-tree の
  container 側で shadow-git を実行）。

## 境界の外側 — best-effort、非復旧

結果が workspace tree 内に land せず、tree capture が決して見ない書き込み:

- **workspace tree 外への書き込み** — `sandboxed_exec` や unsafe-mode Python が
  `/tmp` や `$HOME` などの絶対パスへ書くもの。
- **MCP / 外部プロセスのファイルシステムアクセス** — 構造上外部ゆえ、tree capture の
  完全に外。
- **Noop-sandbox プラットフォーム** — enforcing sandbox の無いプラットフォーム
  （macOS Seatbelt / Linux Landlock 以外）では任意パス書き込みが隔離されず
  （"no isolation enforced"）、tree 内へ制約するものがありません。

これらはクラッシュを生き延びるかもしれませんが、Reyn は **完全性を約束しません** —
tree capture が復元すべきコピーを持たないからです。復旧されねばならない作業は
workspace tree 内に land させるべきです。

**bypass ではない（構造上安全）:** safe-mode `python` op と CodeAct op はファイル
システムへ一切書けません — `open` と `subprocess` が禁止 — ので境界外書き込みを
生みません。

### enforcement の精度について

境界の *本質* は tree-membership です。書き込みを tree 内に保つ正確な機構 — host vs
container の sandbox enforcement 整合、デフォルトの `write_paths`、MCP server の正確な
ファイルシステム reach — は backend / platform 依存の enforcement の細部です。
tree-membership 境界を durable な契約として扱い、per-backend の enforcement はそれを
（精度に差はあれ）支える機構と見なしてください。どこでも一律に enforce されると仮定
しないことです。

## 現状: runtime / workspace の durability 非対称

境界の *scope* とは別に、その *durability* が今どこにあるかを正直に述べます:

- **Runtime substrate（WAL + snapshots）** は **電源断 durable** — fsync-per-append
  契約により、OS クラッシュや電源断でも commit 済みのものは失われません。
- **Workspace content recovery（L2）** は **まだ fsync-ordered でない** — ハードな
  電源断では、捕捉された tree がペアであるべき runtime 状態と乖離しうります。

この非対称は確定した設計判断ではなく **in-progress** です。方向性は L2 を WAL を
鏡映する durability + ordering barrier の下に置き、内側集合全体を（runtime 半分だけ
でなく）電源断 durable にすることです。それが入るまで、完全性保証は clean なプロセス
クラッシュに対して成立し、workspace content の電源断エッジが塞がれつつある gap です。
境界の原則は変わりません — 変わるのは、その durability が現状どこまで届くかだけです。

## 関連

- [Events](events.md) — WAL と fsync-per-append durability 契約
- [タイムトラベル](time-travel.md) — WAL + runtime snapshot 上の reconstruct、L2 を支える shadow-git generation
- [Workspace](workspace.md) — workspace tree そのもの
- [Permission model](permission-model.md) — Control IR file op (L1) を gate する機構
- [クラッシュ復旧 / skill resume](../skills/skill-resume.md) — 復旧機構
