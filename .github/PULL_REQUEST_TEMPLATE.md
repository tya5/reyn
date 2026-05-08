# Pull Request

## Summary

<!-- 1-3 bullets. What changed and why. -->

-
-

## Modification class

<!-- Mark exactly one with [x]. -->

- [ ] 🔵 拡張 (additive — new capability, no breaking changes)
- [ ] 🔴 仕様変更 (breaking — changes existing behavior or contracts)
- [ ] 🟢 不具合修正 (bug fix)
- [ ] 📚 doc 追加 (documentation only)

## Testing

Reyn has a strict, tiered testing policy. Read it before adding or
modifying tests: [`docs/deep-dives/contributing/testing.ja.md`](../docs/deep-dives/contributing/testing.ja.md)
(English: [`docs/deep-dives/contributing/testing.md`](../docs/deep-dives/contributing/testing.md)).

- Which Tier do the new/changed tests belong to? (1: Contract / 2: OS
  invariant / 3: LLM-replay behavior / scaffold)
- New tests added: yes / no — if no, why?
- All tests pass locally: yes / no

## Checklist

- [ ] Tests pass locally (`pytest`).
- [ ] Lint / format clean.
- [ ] No skill-specific strings (phase names, artifact types, fields)
      added to OS code (P7).
- [ ] P1-P8 invariants respected (see `CLAUDE.md`).
- [ ] `CHANGELOG.md` updated if the change is user-visible.
- [ ] No secrets, credentials, or API keys included in the diff.
