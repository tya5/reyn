---
type: tutorial
topic: getting-started
audience: [human]
---

# 01 — Installation

Get a working reyn install in under 5 minutes.

## Prerequisites

- Python 3.11+
- A LiteLLM-compatible model endpoint (OpenAI, Gemini via Google AI, Anthropic, or a local proxy like LiteLLM Proxy)

## Install

```bash
git clone https://github.com/tya5/reyn.git
cd reyn
python -m venv venv
source venv/bin/activate
pip install -e '.[dev]'
```

The `reyn` CLI is now on your PATH.

## Configure a model

reyn picks the model from `reyn.yaml`. The shipped default uses Gemini via a LiteLLM proxy. To use a different provider, edit the `models` map:

```yaml
# reyn.yaml
model: standard
models:
  light:    openai/gpt-4o-mini
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

The shorthand `<value>` above is a literal model string (= contains `/`). Reyn also
ships a built-in catalog (e.g. `claude-sonnet-thinking`, `gemini-flash-lite`) that
you can reference by short name, and a dict form with `extends` for cost variants.
See `reference/config/reyn-yaml.md` and `reference/builtin-models.md` for details.

Then export the matching API key:

```bash
export OPENAI_API_KEY=sk-...
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

!!! warning "Never commit API keys"
    Keys belong only in environment variables. `reyn.yaml` is checked in; put proxy URLs in `reyn.local.yaml` or `~/.reyn/config.yaml` (gitignored).

## Initialize a project

In your working directory:

```bash
reyn init
```

This creates `reyn.yaml` and `reyn.local.yaml.example` if they don't exist.

## Verify

```bash
reyn skills          # lists stdlib + project + local skills
reyn run direct_llm "reyn is a workflow OS for LLMs."
```

If the second command produces a summary and exits cleanly, you're ready for [02 — Chat mode](02-chat-mode.md).

## Troubleshooting

- **`reyn: command not found`** — your venv isn't active. `source venv/bin/activate`.
- **`AuthenticationError`** — the API key env var isn't set, or doesn't match the model in `reyn.yaml`.
- **Proxy connection refused** — start your LiteLLM proxy, or remove `api_base` from `reyn.local.yaml`.
