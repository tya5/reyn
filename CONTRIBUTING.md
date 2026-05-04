# Contributing to Reyn

Thank you for your interest in contributing. Reyn is a predictability-first
agent OS — contributions that preserve correctness and architectural integrity
are welcome. Contributions that violate the invariants in `CLAUDE.md` will
not be merged.

## What contributions are welcome

- Bug fixes with a clear reproduction path
- New stdlib skills (no OS changes required — see P7)
- Test additions that fit Tier 1, 2, or 3 (see Testing policy below)
- Documentation improvements under `docs/`
- Performance work that does not change observable behavior

If you plan a large change, open an issue first to align on scope.

## Development setup

```bash
git clone https://github.com/your-org/reyn.git
cd reyn
pip install -e ".[dev]"
```

The `dev` extra installs `pytest`, `pytest-cov`, `ruff`, and `mypy`.

For LLM-replay tests you need a running LiteLLM proxy (default:
`localhost:4000`). See `docs/en/concepts/workspace.md` for environment
details.

Run the full suite:

```bash
python -m pytest tests/ -v
```

## Testing policy

The normative policy is at
[`docs/en/contributing/testing.md`](docs/en/contributing/testing.md).
Read it before writing or modifying any test. The key rules are:

**Tier model.** Every test belongs to exactly one tier:

- Tier 1 — Contract: external schema boundaries (YAML schemas, Events JSONL
  payloads, public Python API).
- Tier 2 — OS invariant: architectural invariants derived from P1-P8,
  subsystem contracts, and multi-component end-to-end invariants.
- Tier 3 — LLM-replay: single-phase LLM-dependent paths exercised through
  `LLMReplay` fixtures. Multi-phase scenario replay (Tier 3b) is deferred
  pending CLI redesign.
- Tier 4 — Do not write. Anything that does not clearly fit Tier 1-3 belongs
  here.

**No mocks.** Never use `unittest.mock.MagicMock`, `AsyncMock`, or `patch` to
fake collaborators. Use real instances or the `LLMReplay` Fake. Mocks bypass
real API contracts and silently rot.

**No private state assertions.** Never assert on private attributes
(`tracker._daily_tokens`, `mgr._timers["c1"]`). Use the public surface or a
`snapshot()`-style read.

**No algorithm pinning.** Do not pin sort order, dict iteration order,
internal cache structure, or exact whitespace/formatting.

**No snapshot tests** outside `tests/scaffold/`. The only permitted use is
characterization tests for legacy refactors, with mandatory `triggered_by` /
`removed_by` metadata and deletion in the landing PR.

**Docstring tier declaration.** The first line of every test docstring must
declare its tier: `"""Tier 2a: ..."""`.

## Code style

Formatting and linting are enforced by `ruff` (line length 100, rules E/F/I).
Type checking uses `mypy` in non-strict mode. Run before submitting:

```bash
ruff check src/ tests/
ruff format src/ tests/
mypy src/
```

There is no `black` dependency — `ruff format` covers formatting.

## Architectural rules (P1-P8)

Reyn's OS is governed by eight invariants. Violations break the runtime and
will not be merged. The full rationale is in
[`docs/en/concepts/principles.md`](docs/en/concepts/principles.md). The
hard rules:

- **P1** Phase declares only `input_schema` and instructions. It must not
  know its next phase, output schema, or parent skill.
- **P2** Skill declares `entry_phase`, `graph`, and `final_output_schema`.
  Phase connections live in Skill, never in Phase.
- **P3** The OS is the sole runtime engine. Skills and the LLM describe and
  decide; they do not execute.
- **P4** The LLM picks only from OS-provided candidates. No arbitrary next
  phases.
- **P5** All inter-phase data lives in the workspace. Phases read and write
  only through Control IR.
- **P6** Every state change emits an event. The event log is append-only.
- **P7** OS code must not contain skill-specific strings (phase names, artifact
  types, field names). This is the most commonly violated rule — read the
  detection guidance in `CLAUDE.md` before touching `src/reyn/`.
- **P8** Phase instructions describe what/when/domain rules. They must not
  enumerate output artifact fields or describe Control IR format.

New skills must not require OS changes (P7). If your skill seems to need an
OS change, open an issue to discuss before proceeding.

## PR process

1. Fork the repository and create a branch from `main`.
2. Make your change. Include tests appropriate to the tier model.
3. Ensure `ruff`, `mypy`, and `pytest` all pass.
4. Open a pull request against `main` with a description that explains the
   motivation, not just what changed.
5. At least one maintainer review is required before merge.
6. Squash merge is the default; a clean commit history is preferred.

PRs that add OS-layer changes without a corresponding P7 audit will be
returned for revision.

## Commit message style

Reyn uses [Conventional Commits](https://www.conventionalcommits.org/).
Examples from the project history:

```
feat(os): per-turn router invocation cap
fix(os,stdlib): three eval bugs from S2 dogfood
refactor(i18n): output_language Optional[str] — no regional default
docs(concepts): architecture overview update
```

Format: `type(scope): short imperative summary`. Keep the subject under 72
characters. Add a body if the motivation is not obvious from the subject.

Valid types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`.

## License

By contributing, you agree that your contributions will be licensed under the
same MIT License that covers this project.
