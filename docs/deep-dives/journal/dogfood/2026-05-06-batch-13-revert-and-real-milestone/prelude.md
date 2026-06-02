# Batch 13 (revert + real milestone) — Prelude

> batch 11-12 で landing した permission system 関連 fix の **doc 違反を発見**、
> documented design (= iApp 型: declare + approve) への復帰 batch。 dogfood 運用は
> `reyn.local.yaml` pre-approval (= layer 3 mechanism) で対応。 副次的に B11-NEW-2
> V3 wording fix を landing、 N=5 で **真の milestone 確定** を target。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-06 |
| main HEAD (batch 開始時) | `d81e0e2` |
| Test suite | 1022 passed / 2 xfailed |
| 観測 infra | 整備済 |
| Strong model | `gemini-3.1-flash-lite-preview` 使用せず (= cost 10x deferred) |

## Documented design の再確認

`docs/en/concepts/runtime/permission-model.md` で明文化:

```
3 layer:
  1. defaults: project root 配下 read のみ無条件
  2. declared: skill.md frontmatter で declare → user approve (= request, not consent)
  3. project-wide pre-approve: reyn.yaml で operator が grant
```

4 approval source:
- reyn.yaml (= layer 3 / iApp の MDM 相当)
- CLI flag `--allow-untrusted-python` (= operator opt-in、 capability class unlock)
- `.reyn/approvals.yaml` (= persisted approval / iApp granted entitlement)
- startup_guard interactive prompt (= TTY mode のみ)

**Non-interactive runs**: 「approvals must be in place beforehand: either pre-approved
in `reyn.yaml` or persisted to `.reyn/approvals.yaml` from a prior interactive run」
= **prompt 不能 mode で auto-approve しない、 事前承認必須** が documented design。

## 過去 fix の doc 整合性 audit

| commit | doc 整合性 | batch 13 action |
|---|---|---|
| `07ee851` (G13 `--allow-untrusted-python`) | ✅ 整合 (= operator opt-in) | 維持 |
| `f666acb` (G14 glob_files PermissionResolver) | ✅ 整合 (= layer 2 を glob にも適用) | 維持 |
| **`651a053` (G15 non-interactive auto-approve)** | ❌ **doc 違反** | **revert** |
| **`2219b20` (R1 stdlib_root を default zone)** | ❌ **doc 違反** (= layer 1 overreach) | **revert** |
| 「B12-NEW-1 fix」 候補 (= python step non-interactive auto-approve) | ❌ doc 違反の対称拡張 | **却下、 dispatch しない** |

= **G15 / R1 / B12-NEW-1 候補は document に記載なし** な non-documented behavior の
introduction だった。 batch 11-12 で「permission system が complex」 と感じた真因。

## Batch 13 の進め方

### Step 1 (parallel revert): documented design 復帰

```
sonnet R1: revert 651a053 (G15)
   - permissions.py の非 interactive auto-approve 経路削除
   - 関連 test 削除 (= G15 用 7 件)
sonnet R2: revert 2219b20 (R1)
   - permissions.py の stdlib_root default zone 追加削除
   - 関連 test 削除 (= R1 用 6 件)
```

両方 doc 違反の **revert (= 不具合修正扱い)**、 仕様変更ではない。 production user
に影響なし、 dogfood 自動化の挙動だけ変わる。

### Step 2: reyn.local.yaml に dogfood pre-approval

```yaml
# reyn.local.yaml (= operator 個人設定、 git 管理外)
permissions:
  file.read: allow      # dogfood で stdlib path を含む全 read を許可
  python.trusted: allow # trusted python step も pre-approve
```

= **documented layer 3 mechanism** の variant (= operator-personal override)。
real user も同じ pattern (= local config で dev/CI 用 grant) を踏むはず。

### Step 3 (parallel with Step 2): B11-NEW-2 V3 wording fix

```
sonnet R3: router_system_prompt.py に V3 wording (= ABSOLUTE rule + JA examples)
```

これは **🟡 仕様変更**: router routing semantics 強化、 user 視点で 40-50% → 5%
routing-fail 改善 (= R2 diagnose で N-shot 確認済)。 system prompt 改変なので
landing 前に diff 確認の機会を確保。

### Step 4: N=5 stability retest

revert + reyn.local.yaml + V3 wording 全適用後の N=5 で **真の milestone** 確定:
- ≥3/5 complete: real milestone confirmed ✅
- 2/5 complete: improving、 batch 14 で別 blocker fix
- 0-1/5 complete: 再 diagnose

### Step 5: findings + retro

## Prediction

| Step | Top prediction | base rate 根拠 |
|---|---|---|
| Step 1 (revert) | verified 90% | deterministic revert |
| Step 3 (V3 wording) | verified 70-80% | R2 で N-shot 5% rate 既測定 |
| Step 4 (N=5) | 3/5: 35% / 4-5/5: 25% / 0-2/5: 30% / inconclusive: 10% | 真の milestone 確定 60% 確率 |

Brier target: ≤ 0.30 (= batch 10 水準復帰)

## 想定外シナリオ + fall-back

- **revert 後 reyn.local.yaml で chain 完走**: 真の milestone confirmed、 batch 13 wrap
- **revert 後でも別 blocker**: B12-NEW-1 系の真因が別 layer (= skill.md declaration 不足等)、 別 wave で対応
- **production user に影響発生**: 想定外、 即座に scope 限定

## 修正分類 (= 仕様変更 / 不具合修正)

| 修正 | 種別 |
|---|---|
| G15 revert | 🔵 不具合修正 (= documented design 復帰) |
| R1 revert | 🔵 不具合修正 (= 同上) |
| reyn.local.yaml 設定追加 | 🔵 設定追加 (= code 変更なし) |
| V3 wording fix | 🟡 仕様変更 (= router routing semantics 強化) |

= 主要 fix は documented design 復帰、 仕様変更は V3 wording のみで scope 限定。

## 参照リンク

- batch 12 retro: `../2026-05-06-batch-12-real-milestone/retrospective.md`
- documented permission model: `../../en/concepts/runtime/permission-model.md`
- giveup-tracker: `../giveup-tracker.md`
