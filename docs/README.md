# reyn Documentation

Documentation source for the reyn LLM workflow OS. Build with [MkDocs](https://www.mkdocs.org/) + [Material](https://squidfunk.github.io/mkdocs-material/) and the `mkdocs-static-i18n` plugin.

## Languages

- English (default) — `en/index.md`
- 日本語 — `ja/index.md`

## Build locally

```bash
make docs-install   # installs mkdocs + plugins into venv
make docs-serve     # http://127.0.0.1:8000
make docs-build     # builds static site to ./site (strict mode)
```

## Layout

```
docs/
├── en/              # English (Diátaxis: tutorials / how-to / reference / concepts)
├── ja/              # Japanese translations (untranslated files fall back to en)
└── agent/           # Agent-only documentation (English only, no i18n)
```

See [contributing/style-guide.md](en/contributing/style-guide.md) for the writing rules and translation policy.
