---
type: how-to
topic: architecture
audience: [human]
---

# Release process

Maintainer-only checklist for cutting a Reyn release. The Japanese
translation is deferred to a future wave; this page is English-only for now.

The release pipeline is `tag push → build → (TestPyPI | PyPI) → GitHub
Release`. All publishing is driven by the workflow in
`.github/workflows/release.yml` — local `twine upload` is not used.

## Pre-release checklist

Run from a clean `main` working tree:

1. `python -m pytest -q` — green (currently 1452+ tests).
2. `mkdocs build --strict` — no warnings.
3. `ruff check .` — clean.
4. Review `CHANGELOG.md` `[Unreleased]` section. Confirm every entry has
   the right category (Added / Changed / Fixed / Documentation / Removed)
   and that no merged PR is missing.
5. Bump `pyproject.toml` `version` to the new release (e.g. `0.1.0a2`).
   The build job verifies tag-vs-pyproject equality and fails the run if
   they disagree.
6. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.1.0aN] — YYYY-MM-DD`
   (ISO date), insert a fresh empty `## [Unreleased]` block above it, and
   add the new compare-link footer entry.

Commit the bump + changelog rename as a single `chore(release): vX.Y.Z`
commit and push to `main`.

## TestPyPI dry-run (recommended for first publish)

Tag with a `-test` suffix to route through TestPyPI without touching
production PyPI:

```bash
git tag v0.1.0a2-test
git push origin v0.1.0a2-test
```

The workflow runs `build` → `publish-testpypi`. The `github-release` job
is skipped. Verify with:

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            reyn==0.1.0a2
reyn --version
```

If anything goes wrong, delete the tag (`git push --delete origin
v0.1.0a2-test`) and iterate. TestPyPI does not allow re-uploading the
same version, so bump to `-test2` etc. for retries.

## Production release

After the dry-run looks good:

```bash
git tag v0.1.0a2
git push origin v0.1.0a2
```

This triggers `build` → `publish-pypi` → `github-release`. Watch the run
in **GitHub Actions → Release**; the three jobs should be green within
~5 minutes.

Smoke-test the published wheel:

```bash
pip install reyn==0.1.0a2
reyn --version
reyn list-skills
```

## Post-release

1. Confirm the GitHub Release page lists `dist/*.whl` + `dist/*.tar.gz`
   as attached assets, with the CHANGELOG section as the body.
2. Open a follow-up PR that:
   - Bumps `pyproject.toml` `version` to the next dev value (e.g.
     `0.1.0a3.dev0` if you adopt PEP 440 dev markers, otherwise just the
     next planned release).
   - Confirms the new empty `## [Unreleased]` block is in place.
3. Announce the release (HN / Zenn / Twitter) only after smoke-test passes.

## Required secrets

These live in **Settings → Secrets and variables → Actions** and are set
manually by the maintainer. The workflow will fail loudly if they are
missing — that's intentional:

- `PYPI_API_TOKEN` — scoped to project `reyn` on https://pypi.org/.
- `TEST_PYPI_API_TOKEN` — scoped to project `reyn` on
  https://test.pypi.org/. Optional; only needed if you use the `-test`
  flow.

The workflow uses GitHub Environments (`pypi`, `testpypi`) so you can
attach manual approval gates if you want a human-in-the-loop step before
the upload happens.
