---
title: 「全部一気にやれば clean」 という誘惑を抑え、 risk 階層別 phasing で大規模 migration を着地させた話 — docs restructure 3-phase
discovered: 2026-05-08
session-context: docs 体系再構成 (= 2026-05-08) を 1 wave で landing する誘惑を抑え、 構造的制約 (= mkdocs-static-i18n folder mode 不安定) に沿って P1 / P2 / P3 の 3-phase に分割。 各 phase 後に verification (= pytest + ruff + mkdocs strict + grep audit) で gate、 累積 541 files touched / zero regression / 1467 passed 維持で landing
related-commits: []
related-giveup: []
related-memory: [feedback_minimize_speculation, feedback_verify_reproduce_first]
status: stable
---

# 「全部一気にやれば clean」 という誘惑を抑え、 risk 階層別 phasing で大規模 migration を着地させた話

> 「全部一気にやれば clean」 という誘惑を抑え、 risk 階層別 phasing で
> 大規模 migration を着地させた話 — docs restructure 3-phase

## TL;DR

Reyn の docs 体系再構成 (= persona-first 4-section + i18n folder→suffix 移行)
を **1 wave landing の誘惑を抑え、 3-phase 分割** で着地。 構造的制約 (=
mkdocs-static-i18n folder mode の言語 dir 外 move 不安定) を phase boundary
の根拠に採用、 各 phase 後に独立 verification (= pytest + ruff + mkdocs
strict + grep audit) で gate。

累積 **541 files touched、 zero regression、 1467 passed 維持**。 phase 分割
は「キリのいいところで切る」 ではなく「構造制約に逆らわない unit を identify
する」 という pattern に到達。

## 教訓 headline

1. **risk 階層別の phasing で boundary を localize**: 「全部一気」 は merge
   conflict / verification fail の影響範囲が phase 全体に拡散、 phasing で
   各 phase の risk 範囲を独立化。 1 phase 失敗しても前 phase 効果は保持。
2. **phase 境界は構造的制約から導出**: 「キリのいいところで切る」 では弱い。
   docs restructure では mkdocs-static-i18n の folder mode 制約 (= 言語 dir
   外への move 不安定) という構造制約が P1 / P2 と P3 を自然分離した。 制約
   に逆らわずに phase boundary を draw する pattern。
3. **早期 fail で次 phase 着手判断**: 各 phase 後の verification (= pytest +
   ruff + mkdocs strict + grep audit) が gate。 P1 完了後に「全 1467 passed
   + 0 WARNING」 で confidence 醸成、 P2 で同様に。 pre-P3 時点で「これで P3
   risk 取れる」 confidence が phase 累積で積み重なる。
4. **想定外 conflict の局所化**: P3 で発生した「README.md vs index.md
   conflict」 (= 旧 `docs/README.md` と新 `docs/index.md` が同 dir で衝突)
   は phase 単位で localize されたから対処可能だった。 1 wave 一気の場合、
   同 conflict が 540 file の中で埋もれる risk。
5. **sonnet 並列 vs 直列の選択**: phase 内は file-disjoint で並列可能だが、
   phase 間 (= P1 → P2 → P3) は dependency 直列。 「並列性最大化」 ではなく
   「並列性が成立する unit を identify」 が opus の役割。
6. **scope 区切りで sonnet 1 体完走**: 各 phase は 89-224 files 含むが、
   file-disjoint な reorg 作業として 1 sonnet が完結。 過去の経験 (=
   ChatSession refactor 等で 3 sonnet 並列) と異なり、 「reorg 系は
   serialize 寄り、 feature 系は parallel 寄り」 という pattern 観察。

## Background — 1 wave landing の誘惑

researcher session (= worktree `claude/eager-shaw-389d9d`) からの提案を起点
に、 docs 体系再構成 proposal が surface した:

- 元 proposal: `docs/{en,ja}/` 完全ミラー構造を廃止、 persona-first
  4-section + suffix i18n に切替
- 規模: docs/ 配下 540+ files の rename / move を伴う大規模 reorg

**1 wave で一気に landing したい誘惑** (= 中途半端な状態で history が分断
しない、 review が 1 PR で済む) が当然あった。 ここで以下の構造的制約を
identify したことが phase 分割判断の起点:

> mkdocs-static-i18n の folder mode は **言語 dir 外への file move を伴う
> reorg で build 不安定** (= nav resolution + i18n suffix 解決が flat layout
> 前提)。 言語 dir を一気に潰す operation は risk 大。

= 「framework / config の semantic dependency」 が phase 境界の根拠。 制約に
逆らわずに「前半は安全な subtree のみ、 後半で言語 dir 解体」 という分割が
自然に導出された。

## 3-phase 設計

```
P1: build-excluded subtree のみ (= deep-dives/ 配下、 nav 影響ゼロ)
   ↓ verify (= pytest + ruff + mkdocs strict)
P2: 言語 dir 内再編 (= guide/ persona 化、 言語 dir 維持)
   ↓ verify
P3: i18n folder→suffix migration + en/ja root 化 + 残 move
   ↓ verify
```

### Phase scope と risk profile

| phase | scope | risk | file count | sonnet 工数 |
|---|---|---|---|---|
| P1 | build-excluded subtree → `deep-dives/` | 低 (= nav 影響ゼロ) | 224 files | 1 sonnet × 1 pass |
| P2 | tutorials + how-to → `guide/` persona、 言語 dir 維持 | 中 (= nav 再編、 但し i18n folder mode 維持) | 89 files | 1 sonnet × 1 pass |
| P3 | i18n folder→suffix migration + en/ja root 化 + decisions/contributing → deep-dives | 高 (= 全 URL 体系変動 + folder mode 解体) | 224 files | 1 sonnet × 1 pass + opus conflict 解消 |

= P1 で「build-excluded subtree (= deep-dives/) は mkdocs build から除外
されるため、 nav の影響ゼロ」 という構造的 invariant を活用、 lowest-risk
operation から開始。 P3 で初めて「言語 dir 解体」 という irreversible
operation を実行。

## ステップ-by-ステップ narrative

### ステップ 1 — proposal 受領、 判断岐路

researcher proposal は単一 doc で「最終形」 を提示、 implementation strategy
は read として残されていた。 「全 phase 一気 vs 段階分割」 の判断岐路。

### ステップ 2 — opus 分析で構造制約を identify

mkdocs-static-i18n の folder mode 仕様を確認、 「言語 dir 外への move を
伴う build は不安定」 という制約を identify。 段階分割が必須と判断、 制約
境界 (= 言語 dir 内 vs 外) で P1 / P2 と P3 を自然分離。

### ステップ 3 — 3-phase 設計

```
P1 = build-excluded subtree only (= deep-dives/、 nav 影響ゼロ)
P2 = 言語 dir 内再編 (= guide/ persona、 i18n folder mode 維持)
P3 = i18n suffix migration + en/ja root 化 + 残 move
```

各 phase の deliverable を独立 verifiable な metric で定義:
- P1 後: 全 1467 passed + mkdocs build --strict 0 WARNING + grep `docs/en/concepts` 残存 0
- P2 後: 同上 + nav の persona 化 reflect
- P3 後: 同上 + i18n suffix で 全 page resolve + en/ja root 化

### ステップ 4 — 各 phase で sonnet 1 体に dispatch

phase scope に絞った precise brief + verification commands + return format
を 1 sonnet に渡し、 89-224 files の機械的 rename / move を完走させる。
phase 間 (= P1 → P2 → P3) は dependency 直列、 1 phase 完了後に opus が
verification + 次 phase brief を準備。

### ステップ 5 — 全 phase landing

3-phase 累積で 541 files touched、 zero regression、 1467 passed 維持。 P3
で 1 件の想定外 conflict (= 旧 `docs/README.md` と新 `docs/index.md`) が
発生したが、 P3 phase 単位に localize されていたため opus が in-place で
解消、 P1 / P2 効果は無傷。

## Methodology checklist (= operational)

```markdown
- [ ] 大規模 migration を planning する時、 1 wave で landing する誘惑を
      意識的に抑える
- [ ] 構造的制約 (= framework / config の semantic dependency) を identify、
      制約境界で phase 分割
- [ ] 各 phase の deliverable を独立 verifiable な metric で定義 (= test
      pass / build pass / grep zero 等)
- [ ] 各 phase 完了後に commit + push で history 保持、 次 phase まで
      cooling-off 可能に
- [ ] phase 間 dependency を明示 (= P3 が P2 完了を前提とする等)、 中断時の
      resume point 明確化
- [ ] sonnet dispatch 時、 phase scope に絞った precise brief + verification
      commands + return format を渡す
```

## Reyn-specific tooling reference

phasing と相性の良い既存 infra:

- `git mv` で history 保持 (= 各 phase 内で大量 file rename、 blame chain
  維持)
- `mkdocs build --strict` で各 phase の boundary 確認 (= broken link / nav
  inconsistency が即 detect、 0 WARNING gate を通せば次 phase 着手可)
- `grep -rn '<old path>' docs/ src/` audit で phase 後の path 残存ゼロ
  確認 (= dangling reference の早期 detect)

## Universal pattern — 「並列性最大化」 ではなく「並列性が成立する unit
を identify」

過去の経験 (= ChatSession refactor 等で 3 sonnet 並列) と本 case の対比で
以下の pattern が観察された:

| 作業 type | 並列性 | 理由 |
|---|---|---|
| feature 開発 (= 独立 module 追加) | 並列寄り | file-disjoint + 同時 verify 可能 |
| reorg / migration | 直列寄り | phase 間 dependency 直列、 中間状態が build 不可 |
| bug fix | mixed | 真因 isolation 段階は直列、 fix dispatch は並列可 |

= 「sonnet 並列を default」 ではなく、 **「並列性が成立する unit (= phase
内、 file-disjoint、 中間 build 可能)」 を identify する判断 step** を opus
が握る。 reorg 系は phase 内並列はあり得るが phase 間直列は強制される。

## 適用可能性 (= future work hint)

### 同 pattern が活きそうな場面

1. mkdocs / sphinx 等の docs 体系再構成 (= nav + i18n + section 再編が
   絡む)
2. モジュール大規模 rename / package 階層変更 (= import path が全域に
   散る)
3. DB schema migration の段階適用 (= rolling deploy + backfill + cutover)
4. monorepo の workspace 分割 (= file move + build config 変更 + import
   path 変動)

### 不適用な場面

1. 1 file 内の局所 refactor → phasing overhead が cost 上回る
2. 完全可逆 operation で迅速 revert 可能 → 1 wave で OK
3. 構造的制約が無く全域 mechanical → 大量並列で短時間着地 (= phasing
   不要)

## 制約 / 本 insight が claim しないこと

- **「phasing が常に高速」 とは claim しない**: phase 間 verification の
  overhead が小規模 migration では cost 上回る。 規模 / risk と phasing
  overhead の trade-off は case-by-case 判断。
- **「sonnet 並列が常に劣る」 とも claim しない**: feature 系作業 / 完全
  独立 module 追加では 3 sonnet 並列が optimal な事例多数。 本 insight の
  claim は「reorg 系では並列前提を疑え」 に限定。
- **「3-phase が universal optimum」 とも claim しない**: phase 数は構造
  制約から導出される、 case ごとに 2-phase / 4-phase もあり得る。

## References

### 同 session の sibling insights
- [「25/25 refuted」 false alarm — plumbing fix + observe-first で投機的
  description rewrite を回避](2026-05-08-r1-false-alarm-observe-first-saved.md)
  (= 同 author / 同 day、 observe-first discipline と相補的: 観測 vs phasing
  はどちらも「推測 / 楽観で wave を進めない」 という共通 spirit)

### 関連 process docs
- [dogfood discipline](../../contributing/dogfood-discipline.md) (= 9 原則
  framework と integrate 可能、 phasing は新規 universal pattern として
  追加候補)

### 関連 ADR
- 該当なし (= 設計判断ではなく process methodology)

### memory pointer
- `feedback_minimize_speculation.md` (= 1 仮説 1 修正 1 検証、 phasing は
  同 spirit を migration に適用したもの)
- `feedback_verify_reproduce_first.md` (= verify-first / reproduce-first
  discipline、 phase ごとの verification gate に直接対応)
