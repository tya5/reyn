---
title: Fix を 「仕様変更 / 不具合修正 / doc 追加 / revert」 の 4 区分で dispatch 前に label する規律 — fix accumulation drift を防ぐ classification as discipline
discovered: 2026-05-08
session-context: batch 13 era (= 2026-05-06、 permission 関連 fix 累積 → simplicity smell test trigger → 2 fix revert) で確立した規律。 batch 14 で「全 3 fix が 🔵 不具合修正、 仕様変更ゼロ」 declaration、 transparency convention に発展。 既存 dogfood-discipline Principle 7 (= 2 区分: spec change / bug fix) を 4 区分 (= + doc 追加 + revert) + reject path に拡張、 revert を first-class fix category として discipline 化
related-commits:
  - 1408f42  # batch 13 revert wave 1
  - b92a22c  # batch 13 revert wave 2
related-giveup: [G15, G21]
related-memory: [feedback_minimize_speculation, feedback_verify_reproduce_first]
status: stable
---

# Fix を 「仕様変更 / 不具合修正 / doc 追加 / revert」 の 4 区分で dispatch 前に label する規律

> Fix を 「仕様変更 / 不具合修正 / doc 追加 / **revert**」 の 4 区分で
> dispatch 前に label する規律 — fix accumulation drift を防ぐ classification
> as discipline

## TL;DR

LLM-driven dev workflow で fix が累積するとき、 **各 fix の意図と影響範囲が
曖昧** だと user-visible behavior change と internal correctness fix が混在し、
後で「これは何の fix だっけ?」 が起こる (= fix accumulation drift)。 batch 13
era で確立した規律: **dispatch 前に 4 区分 (= 仕様変更 🟡 / 不具合修正 🔵 /
doc 追加 ⚪ / revert ⛔)** で label し、 documented design 整合性 audit を
gate に組み込む。 batch 14 では「全 3 fix が 🔵 不具合修正、 仕様変更ゼロ」
declaration が transparency convention に発展。 特に **revert を first-class
fix category** として扱うこと、 + dispatch 前 **reject path** を discipline
化することで、 fix accumulation drift を抑止する。

## 教訓 headline

1. **fix を dispatch 前に classify することが、 後の audit / revert 判断を
   可能にする**: label 不在 fix は意図不明で revert しにくい (= 「巻き戻すと
   何が壊れる?」 が読めない)。 classification は future audit / future revert
   の precondition。
2. **4 区分 (= spec / bug / doc / revert) は 既存 2 区分 (= bug fix vs feature)
   を超えた pedagogical model**: 既存 Principle 7 (= spec change vs bug fix の
   2 区分) は dispatch 時点の audit には十分だが、 「過去 fix の取消」 と
   「behavior 不変の doc 更新」 を独立 category として扱う pedagogical 拡張で
   fix wave の retro / grep が改善。
3. **revert as first-class fix category**: fix の正当性が消えた時に「黒歴史化」
   せず明示的に巻き戻す path を持つ。 revert を「失敗の証」 でなく「discipline
   の一部」 として記録することで、 dispatch 時 experimentation が cheap になる
   (= 「念のため land しておく」 でなく「dispatch して検証 → 違ったら revert」)。
4. **fix candidate 却下 (= reject path) も discipline**: 「対称性破壊する fix」
   「documented design 違反 fix」 は dispatch 前に却下、 batch 12 B12-NEW-1 例
   のように production user impact ゼロで fix accumulation drift を阻止できる。
5. **production-user-facing change の transparency convention**: 🟡 / 🔵 emoji
   + commit message + retro section で明示することで future audit を grep-able
   に保つ。 「この PR で landed した user-visible change は何?」 が即引ける。
6. **feature-verify documentation pattern**: fix 後 separate doc で「真に
   effective か」 を independent verify、 fix と verify を切り離すことで
   verify-first principle と integrate。

## Background — 規律 不在 era で何が起きたか

Reyn の dogfood batch 9–12 で permission 関連 fix を累積、 「対称性拡張 (=
G15 stdlib path + G21 CWD)」 と称した fix 群が landed。 各 fix は局所的に
test pass + lint pass + mkdocs strict pass、 automated audit は green。

ところが batch 13 era で **simplicity smell test** (= sibling insight
[2026-05-08-simplicity-smell-test-recalibration.md](2026-05-08-simplicity-smell-test-recalibration.md)
参照) が trigger され、 user の plain-language probe で 2 fix が documented
design 違反 (= ADR-0020 skill-only-permissions に反する exception 追加) と
判明、 revert に至った。

revert 後、 retro で次の課題が surface した:

> revert 対象 fix と「正当な fix」 を区別する label が無かったため、 batch 9–12
> retro 時点で「fix」 として一括処理された。 dispatch 前 classification があれば、
> simplicity test を待たずに B12-NEW-1 (= 対称性破壊候補) は却下できた。

= 「fix dispatch 前に classification 必要」 という discipline が確立した起点。

## chronological narrative

### Region 1 — 規律 不在 era (= batch 9–12)

- 「fix」 という単一 category で全 patch を dispatch
- documented design 違反 vs spec change の境界曖昧 (= G15 / G21 / 関連 fix
  群が混在)
- 各 fix は test green / lint green / mkdocs strict green、 automated audit
  は通過
- 累積で incoherence (= sibling insight の simplicity test trigger 条件) が
  発生、 user の probe で初めて露呈

### Region 2 — 規律 確立 (= batch 13)

- 2 件 revert (= `1408f42` revert wave 1、 `b92a22c` revert wave 2) で「fix
  も巻き戻せる」 経験を獲得
- retrospective で 4 区分 label を formal 化:
  - 🟡 仕様変更 (= user-visible behavior change、 doc update 同時必要)
  - 🔵 不具合修正 (= 意図と実装の乖離訂正、 doc update 不要)
  - ⚪ doc 追加 (= behavior 不変、 documentation のみ)
  - ⛔ revert (= 過去 fix の取消、 ADR / commit message で trace)
- 各 commit message 本文に classification を埋め込む convention 採用

### Region 3 — refinement (= batch 14)

- batch 14 で fix dispatch 前に **「全 3 fix が 🔵 不具合修正、 仕様変更ゼロ」**
  declaration、 retrospective に明記
- 「仕様変更ゼロ batch」 という transparency convention が確立、 future audit
  で「いつ user-visible change が landed したか」 が即追跡可能に
- feature-verify entries (=
  `docs/deep-dives/journal/feature-verify/2026-05-06-pr-model-spec-passthrough.md`
  等 3 件) で 🟡 / 🔵 / 🟢 marker を使用、 commit message + journal 双方に
  classification を残す

### Region 4 — reject path 確立 (= B12-NEW-1 例)

- batch 13 で B12-NEW-1 (= 「対称性 symmetric 拡張」 候補) を dispatch 前 audit
  で却下:
  - 候補は「G15 / G21 と symmetric な拡張」 として surface
  - dispatch 前 classification で「仕様変更系 + 対称性破壊」 と判定
  - documented design (= ADR-0020) と矛盾するため却下
- 「fix 候補は dispatch 前に classify、 仕様変更系 + 対称性破壊なら却下」
  という reject path が確立
- reject も dogfood discipline の一部として記録 (= retrospective findings
  section)、 後の grep で「却下された fix 候補」 が見える
- production user impact ゼロ、 fix accumulation drift を阻止

## Universal lessons

### Lesson 1 — 単一 category 「fix」 は audit / revert / doc update の意思決定を不可能にする

各 action (= audit、 revert、 doc update、 user notification) は classification
依存。 「fix」 という単一 category だけで treat すると、 retro 時点で「この
fix は user-visible だっけ?」 「documented design に書かれてる?」 が読めず、
revert 候補の boundary も draw できない。 classification は **action の
prerequisite** であり、 後付けでは boundary が曖昧化する。

### Lesson 2 — 4 区分 model の意義

| label | 意味 | doc update | user notification |
|---|---|---|---|
| 🟡 仕様変更 | user-visible behavior change | 必要 | 必要 |
| 🔵 不具合修正 | 意図と実装の乖離訂正 | 不要 (= spec 既存) | 不要 |
| ⚪ doc 追加 | behavior 不変、 documentation のみ | (= 本体) | 不要 |
| ⛔ revert | 過去 fix の取消 | ADR / commit message | 元 fix が user-visible だった場合のみ |

= 4 column matrix が「label ごとに次に何をすべきか」 を直接 derive。 既存
2 区分 (= spec change / bug fix) は dispatch 時 audit には十分だが、 「過去
fix の取消」 と「behavior 不変の doc 更新」 を独立 category として扱う
pedagogical 拡張で retro / grep の解像度が上がる。

### Lesson 3 — revert as first-class fix の意義

「巻き戻せる」 confidence が、 fix dispatch の experimentation を可能にする。
revert を first-class category として扱うと:

- 「念のため land しておく」 でなく「dispatch して検証 → 違ったら revert」
  が cheap な選択肢になる
- revert は「失敗の証」 でなく「discipline の一部」、 retrospective で正面
  から記録できる (= 隠さない)
- revert commit message に「元 commit hash + 却下理由 + 真の resolution path」
  を記載することで、 future contributor が同 design 失敗を再演しない

batch 13 の `1408f42` / `b92a22c` 2 件 revert は、 後続の batch 14
「仕様変更ゼロ batch」 declaration を可能にした precondition。

### Lesson 4 — reject path の意義

dispatch コストでなく audit コストで早期検出。 dispatch 後の fix 累積で
incoherence が出る pattern (= simplicity test trigger) は、 各 fix が局所的
に正しくても発生する。 reject path = 「dispatch 前に却下する path」 を
discipline 化することで:

- production user impact ゼロで fix accumulation drift を阻止
- B12-NEW-1 のように「対称性 symmetric 拡張」 の trap (= 「対称性は美しい」
  という美学的 attractor) を documented design 整合性 audit で阻止
- reject 記録は future grep で「同じ候補が再浮上した時」 の cross-ref と
  なる

### Lesson 5 — transparency convention は future audit の grep-ability を確保

🟡 / 🔵 emoji を commit message に inline すると、 `git log --grep='🟡'` で
「user-visible change の commit のみ」 を即抽出できる。 retrospective の fix
wave section で「全 N fix の classification 内訳」 を表 化 (= 「全 🔵 / 仕様
変更ゼロ batch」 のような transparency declaration) することで、 「この
release で何が user-visible に変わった?」 が release notes 自動生成 level の
解像度で残る。

### Lesson 6 — feature-verify documentation = fix と verify の独立 dispatch

fix 後 separate doc で「真に effective か」 を independent verify する pattern
(=
`docs/deep-dives/journal/feature-verify/2026-05-06-pr-model-spec-passthrough.md`
等 3 件)。 fix と verify を切り離すことで:

- fix dispatch の意図と「真に effective か」 を独立に評価可能
- verify-first principle (= memory `feedback_verify_reproduce_first.md`) と
  integrate
- fix が landed しても verify が pending なら「provisional」、 verify 後に
  「stable」 に格上げする lifecycle を classification と組み合わせ可能

## Methodology checklist (= operational)

```markdown
- [ ] Fix を dispatch する前、 「これは仕様変更 / 不具合修正 / doc 追加 /
      revert / reject のどれか」 を 1 文で declare
- [ ] 仕様変更 (= 🟡) の場合、 同時に doc / changelog 更新の TODO を作る
      (= 仕様変更で doc update 漏れは drift の主因)
- [ ] 不具合修正 (= 🔵) の場合、 「意図と実装の乖離」 を commit message で
      説明 (= 「fix bug X」 だけでは弱い、 元の意図を明記)
- [ ] revert (= ⛔) の場合、 元 commit hash + 却下理由 + 真の resolution
      path を commit message に記載
- [ ] reject (= dispatch 前却下) の場合、 retrospective / journal に「却下
      された候補と理由」 を記録 (= 後で同じ候補が再浮上した時の cross-ref)
- [ ] commit message format に classification emoji (= 🟡 / 🔵 / ⚪ / ⛔)
      を inline、 git log で grep-able に
- [ ] retrospective の fix wave section で「全 N fix の classification 内訳」
      を表 化 (= 「全 🔵 / 仕様変更ゼロ batch」 のような transparency
      declaration)
- [ ] feature wave 後、 separate verify doc を書く (= fix と verify を独立
      dispatch、 「真に effective か」 を独立記録)
- [ ] 新規 fix 候補 が surface したら、 documented design (= ADR / spec)
      との整合性を classification の前段で audit
- [ ] documented design 違反 fix が surface したら、 dispatch でなく spec
      改訂 (= ADR amendment) か reject か revert path のいずれかを選択
```

## Reyn-specific tooling reference

phasing と相性の良い既存 infra:

- commit message convention: `fix(<scope>): <description>` + body に
  classification (= e.g., `🔵 bug fix — intent vs impl drift...`)
- retrospective fix wave section の format (=
  `docs/deep-dives/journal/dogfood/2026-05-06-batch-14-stability-extension/retrospective.md`
  参照)
- feature-verify entries (=
  `docs/deep-dives/journal/feature-verify/`) の 🟡 / 🔵 / 🟢 marker
- giveup-tracker entry status (= active / resolved / tracker / revisiting)
  との連携 (= classification と status は orthogonal、 「resolved 🔵」 や
  「revisiting ⛔」 のように combine)

## sibling insight との相補性

本 insight と sibling
[2026-05-08-simplicity-smell-test-recalibration.md](2026-05-08-simplicity-smell-test-recalibration.md)
は **classification と simplicity test の循環** を構成する:

- **simplicity test が classification の trigger**: 累積 fix が user の plain-
  language probe で incoherent と判明 → 「これらの fix は何だったのか?」 を
  retrospect で classify する強制力が発生
- **classification が simplicity test の resolution path**: 4 区分 + revert
  + reject path を discipline 化することで、 simplicity test trigger 時に
  「どの fix を revert するか」 「どの fix を spec 改訂で吸収するか」 が
  documented design 整合性 audit から direct に decide できる

= 両 insight は単体でも valid だが、 combine で「累積 fix で歪む → 露呈する
→ 巻き戻す → 再発防止」 という full cycle を constitute する。

## 適用可能性 / 不適用条件

### 適用

1. 多 commit を含む PR / wave 形式で dispatch する project (= dogfood batch
   等)
2. documented design (= ADR / spec) を持つ project (= revert / reject 判断
   の base)
3. fix 累積で incoherence risk ある system (= LLM-driven dev workflow、
   permission system のような cross-cutting concern を持つ system)

### 不適用

1. single-commit / single-purpose fix が大半の project (= classification
   overhead が利益を上回る)
2. doc ない project (= revert / reject 判断 base 不在、 classification しても
   action 不能)
3. 完全 mechanical fix のみ (= sweep / format / typo fix の場合、 4 区分は
   過剰)

## 制約 / 本 insight が claim しないこと

- **「4 区分が universal optimum」 とは claim しない**: project ごとに 3 区分
  / 5 区分もあり得る、 本 insight は Reyn dogfood context での pedagogical
  expansion を主張
- **「全 fix に classification 必須」 とも claim しない**: trivial fix (= typo、
  format、 dependency bump) は classification overhead が cost 上回る case
  あり、 wave 形式の fix dispatch に focus
- **「revert を first-class にすれば fix 累積 drift がゼロ」 とも claim しない**:
  本 insight は drift を「抑止」 する規律であり、 「ゼロ化」 する mechanism は
  別軸 (= sibling simplicity test 等) と組み合わせ要

## References

### 同 session の sibling insights
- [累積 fix で歪んだ system を、 user の plain-language probe が露呈した話 —
  simplicity smell test as recalibration mechanism](2026-05-08-simplicity-smell-test-recalibration.md)
  (= simplicity test が classification の trigger / classification が
  simplicity test の resolution path、 相補的)
- [「全部一気にやれば clean」 という誘惑を抑え、 risk 階層別 phasing で大規模
  migration を着地させた話](2026-05-08-phased-migration-risk-reduction.md) (=
  同 author / 同 day、 phase boundary discipline は本 insight の classification
  discipline と spirit が共通)

### 関連 process docs
- [dogfood discipline](../../contributing/dogfood-discipline.md) Principle 7
  (= 修正分類明示、 既存 2 区分: spec change vs bug fix。 本 insight は + doc
  追加 + revert の 2 category と reject path を pedagogical 拡張)

### 関連 batch retros
- `docs/deep-dives/journal/dogfood/2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`
  (= 4 区分 label formalization + revert wave 2 件)
- `docs/deep-dives/journal/dogfood/2026-05-06-batch-14-stability-extension/retrospective.md`
  (= 「全 3 fix が 🔵 不具合修正、 仕様変更ゼロ」 declaration の初出)
- `docs/deep-dives/journal/dogfood/2026-05-06-batch-12-real-milestone/findings/B12-R2-diagnosis.md`
  (= reject path 例、 B12-NEW-1 dispatch 前却下 case)

### 関連 feature-verify
- `docs/deep-dives/journal/feature-verify/2026-05-06-pr-model-spec-passthrough.md`
- `docs/deep-dives/journal/feature-verify/2026-05-06-pr-model-spec-extends-deep-merge.md`
- `docs/deep-dives/journal/feature-verify/2026-05-06-pr-time-travel-replay-compare.md`
  (= 3 件、 🟡 / 🔵 / 🟢 marker と verify documentation pattern 例)

### 関連 ADR
- ADR-0020 (= skill-only-permissions、 spec change の好例、 batch 13 revert
  の base)
- ADR-0023 amendment (= 仕様変更の audit document 化 pattern)

### memory pointer
- `feedback_minimize_speculation.md` (= 1 仮説 1 修正 1 検証、 classification
  は同 spirit を fix wave に適用)
- `feedback_verify_reproduce_first.md` (= verify-first / reproduce-first、
  feature-verify documentation pattern と直接対応)
