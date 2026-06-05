# reyn base image (#1324) — minimal generic agent runtime.
#
# Philosophy (OpenClaw / Hermes-aligned): keep the base SMALL; a task extends it
# at runtime via the launcher's setup_command. Heavier, non-universal toolchains
# (e.g. build-essential for C-extension builds, ~200MB+) are intentionally NOT
# baked — use `--image` with a custom image, or add them via setup_command,
# when a task actually needs them.
#
# Built on demand by reyn.environment.container_launcher (local-build-on-demand,
# no registry). A registry-published variant is a tracked #1324 follow-up.
FROM python:3.12-slim

# Near-universal agent tooling: git (repo ops), curl + ca-certificates
# (network fetches / TLS verification). apt lists removed to keep the layer small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root default user. The launcher also passes `--user <host uid:gid>` at run
# time (which overrides this); this USER is the safe fallback when it does not.
RUN useradd --create-home --shell /bin/bash reyn
USER reyn
WORKDIR /workspace
