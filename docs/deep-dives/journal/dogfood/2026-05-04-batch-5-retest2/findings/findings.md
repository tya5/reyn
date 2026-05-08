# Batch 5 Retest 2 — Findings

> B5-H1 + H2 fix 後の動作確認。 B4-H1 narrator reply 経路は ✅ 機能確認、
> 一方 attractor は B2-H1 と同じ位置 (= describe→stop) で 3 度目の発生。
> 「prompt rule 追加路線では押さえきれない領域」 が明確化した瞬間。

**Date**: 2026-05-04
**HEAD at run**: `ca116f3` + `fe91321` (B5-H1 + B5-H2 applied)
**Important caveat**: 本 retest は **G2 fix (`763c86c`、 copy_to_work preprocessor 化) が
landing する前** に走った。 B5R2-H2 (= copy_to_work 0-byte write) は
LLM-driven 版の最後の症状であり、 **G2 fix で構造的に解消されている見込み** —
batch 6 で再 verify が必要。
**Scenarios**: A (curry/specialist) + B (skill_improver eval cascade)

## ハイライト narrative

### attractor の三度目の出現 — B5R2-H1

batch 2 で B2-H1 (`describe → 停止`) を `83bad83` で塞いだ。 batch 3 で
B3-H1 (`list → 停止`) を `48676ad` で塞いだ。 batch 5 で過剰 consolidation で
B3-H1 fix が破壊され、 `ca116f3` で再構築。 そして batch 5 retest 2 で:

```
specialist RouterLoop:
  list_skills("")              → ok
  list_skills("general")       → 10 skills
  describe_skill("direct_llm") → ok ✅ (= ca116f3 で 1 段階前進)
  agent_message_sent           ← invoke_skill 呼ばず exit
```

**B2-H1 と同じ位置で 3 度目の発生**。 `83bad83` の MUST rule が今も prompt に
あるにも関わらず、 weak LLM はそれを honor しないケースが出てきた。 これで
**「prompt rule に依存する戦略では完封できない」** が確定。 batch 6 では
構造的解 (= OS 層 state machine で discovery 後 state を track) への pivot が
必要になる。 詳細は [B5R2-H1](B5R2-H1-describe-skill-stop.md)。

### B4-H1 fix の effectiveness が **遂に** verify された

Scenario B で skill_improver chain を回したとき、 score=0.0 summary が
narrator 経由で user に届いた (= 内容は失敗報告だが、 routing として narrator
reply が `_router_loop_agent_replies` に到達 + default で user 表示される
経路が機能)。 batch 4 で fix した `ffc9b4a` の e2e effectiveness が batch 5
retest 2 でようやく確認できた。 「最後の 1 cm」 問題は (この経路では) 解消。

## Fix verification summary

| Fix | Target bug | Status | Evidence |
|-----|-----------|--------|----------|
| B5-H1 (`ca116f3`) | specialist stops after list_skills | partial | specialist now reaches describe_skill but **not invoke_skill** — 新たな describe→stop attractor が露呈 |
| B5-H2 (`fe91321`) | eval run_target KeyError 'name' | confirmed (prompt side) | run_target が `skill:` field 使うようになった ✅、 ただし下流で別 root cause により同 error 観測 → B5R2-H2 |
| B4-H1 (narrator reply) | skill result not reaching user | **confirmed** ✅ | narrator が score=0.0 summary を user に届けた |
| G2 (`763c86c`) | copy_to_work LLM-driven attractor | **未検証** (本 retest 時点で未 landing) | batch 6 で post-G2 retest 必要 |

## New findings

| ID | Severity | Description | Status |
|----|----------|-------------|--------|
| [B5R2-H1](B5R2-H1-describe-skill-stop.md) | HIGH | **describe_skill→stop attractor**: gemini-2.5-flash-lite が `describe_skill` 後に `invoke_skill` 呼ばず exit。 B5-H1 fix で「list 後 describe」 までは到達するも、 「describe 後 invoke」 の MUST bullet が honor されない | open — batch 6 で **OS 層 state machine** 検討 |
| B5R2-H2 | HIGH | copy_to_work が source を read するが write op で content を省略 → 0-byte file → parse_skill 失敗 → score 0.0 | **likely fixed by G2** (`763c86c`、 preprocessor 化で write content の LLM 経由判断を排除) — batch 6 で確認 |

## 観測上の note

- **infrastructure**: piped input で `reyn chat` を回す場合、 `/quit` 前に
  sleep が必要 (= 非同期 peer agent の routing 完了を待つ)。 dogfood rig の
  pexpect timing 改善余地
- **B5-M1 (parallel invoke)**: 再現確認、 [giveup G3](../../giveup-tracker.md) で
  managed
- **B4-H1 fix の effectiveness が証明された**: B5R2 で narrator reply が
  agent_replies → default → user に到達する経路を観測

## Scenario files

- `prelude.md`
- `B5R2-A.md` (= curry recipe scenario raw observation)
- `B5R2-B.md` (= skill_improver scenario raw observation)

## 教訓

- **B5R2-H1** は B3-H1 (list→stop) と同 family の variant: 「discovery step
  間の commit obligation が weak LLM では各段階で漏れる」 — single bullet
  単位で対処を続けると bullet 数が線形増加 (= G1 / [feedback_prompt_design](../../../../../.claude/projects/-Users-yasudatetsuya-Workspace-junk-claude-sandbox-sandbox-2/memory/feedback_prompt_design.md) の bloat 警告と直結)。 構造的解は code-side で
  「discovery 後の状態遷移を OS が gate」 する設計
- **B5R2-H2 → G2 連動**: 「同じ error message を 2 つの root cause が共有」
  していたため、 fe91321 fix だけで解消したと誤認しかけた。 error message
  の specificity を上げる (= `'name'` でなく context 含む) と root cause
  分離が早まる
- **fix wave と検証 wave の HEAD 整合性**: 並列で fix と retest を回すと、
  retest が古い HEAD で実行され「fix 効果未検証」 のまま残る。 sequential
  運用 (= fix landing 後に retest dispatch) を batch 6 で徹底
