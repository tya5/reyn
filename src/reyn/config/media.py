"""reyn.config.media — media config: Voice/Multimodal/Web/WebFetch. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


@dataclass
class VoiceConfig:
    """`voice:` — chat TUI voice-input (Whisper) settings.

    Lazy-loaded only when the user presses the record key (Ctrl+R) so the
    optional deps (`sounddevice`, `faster-whisper`) stay opt-in. See the
    user guide at `docs/guide/for-skill-authors/enable-voice-input.md`.

    Defaults reflect Reyn's Japanese-enterprise focus (project_reyn_vision):
    `language="ja"` so short clips don't get auto-detected as a wrong
    language and produce empty transcripts. Set `language: ""` (empty
    string) or `null` in YAML to opt back into auto-detect.
    """
    enabled: bool = True              # set False to hard-disable Ctrl+R even if deps installed
    model: str = "small"              # tiny | base | small | medium | large-v3
    language: str | None = "ja"       # ISO code; "" or null in YAML = auto-detect
    device: str = "cpu"               # cpu | cuda  (faster-whisper has no metal backend
                                      # — "auto" silently picks the wrong thing on
                                      # some Mac setups, so default to explicit cpu)
    compute_type: str = "int8"        # int8 | float16 | float32
    sample_rate: int = 16000          # Whisper expects 16 kHz mono
    cpu_threads: int = 4              # 0 = OpenMP default (= os.cpu_count()); pinning
                                      # to 4 on Mac avoids the OpenMP/Python-threading
                                      # deadlock seen with high core counts on Apple
                                      # Silicon. Override per-machine if needed.
    num_workers: int = 1              # parallel transcribe streams; we only ever run
                                      # one at a time, so 1 keeps memory + threads low
    max_duration_s: float = 300.0     # auto-cancel recordings longer than this


@dataclass
class WebFetchConfig:
    """`web.fetch:` — SSL verification settings for web_fetch and MCP registry.

    Priority order (highest to lowest):
      1. ``ca_bundle`` set → use that file path as the CA bundle (``verify=<path>``).
         Satisfies corporate MITM proxy / custom PKI use cases.
      2. ``verify_ssl: false`` → disable SSL verification entirely (``verify=False``).
         Use only in controlled environments where certificate validation is
         intentionally bypassed.
      3. ``verify_ssl: true`` → force SSL verification regardless of env vars.
      4. Both unset (``None``) → fall through to ``SSL_VERIFY`` env var →
         ``litellm.ssl_verify`` → ``SSL_CERT_FILE`` → ``True`` (default).

    Fields:
        verify_ssl:
            ``True`` forces verification. ``False`` disables it. ``None``
            (default) delegates to the env-var / litellm fallback chain.
        ca_bundle:
            Absolute path (or path relative to cwd) of a CA bundle PEM file.
            When set, takes priority over ``verify_ssl`` and env vars.
        max_download_bytes:
            #1913: hard ceiling on the HTTP response body downloaded by
            ``web_fetch``, BEFORE the body is materialized into memory. A
            response whose ``Content-Length`` exceeds this — or that streams past
            it without one — is rejected (``status="too_large"``), preventing an
            unbounded-memory DoS from a hostile URL (including a benign URL that
            redirects to a huge payload). Distinct from ``WebFetchIROp.max_length``
            (which caps the *extracted text* only AFTER the full body is loaded).
            Default 10 MiB.
    """
    verify_ssl: bool | None = None
    ca_bundle: str | None = None
    max_download_bytes: int = 10 * 1024 * 1024


@dataclass
class WebConfig:
    """`web:` — web operation settings.

    Aggregates ``web.fetch`` sub-section. Extend here when ``web.search``
    gets its own knobs.
    """
    fetch: WebFetchConfig = field(default_factory=WebFetchConfig)


def _build_web_fetch_config(raw: object) -> WebFetchConfig:
    """Parse the ``web.fetch:`` sub-section."""
    if not isinstance(raw, dict):
        return WebFetchConfig()
    ca_bundle_raw = raw.get("ca_bundle")
    ca_bundle = str(ca_bundle_raw) if ca_bundle_raw is not None else None
    verify_ssl_raw = raw.get("verify_ssl")
    if verify_ssl_raw is None:
        verify_ssl: bool | None = None
    else:
        verify_ssl = bool(verify_ssl_raw)
    defaults = WebFetchConfig()
    try:
        max_download_bytes = int(raw.get("max_download_bytes", defaults.max_download_bytes))
        if max_download_bytes <= 0:
            max_download_bytes = defaults.max_download_bytes
    except (TypeError, ValueError):
        max_download_bytes = defaults.max_download_bytes
    return WebFetchConfig(
        verify_ssl=verify_ssl,
        ca_bundle=ca_bundle,
        max_download_bytes=max_download_bytes,
    )


def _build_web_config(raw: object) -> WebConfig:
    """Parse the ``web:`` section. Empty / missing returns full defaults."""
    if not isinstance(raw, dict):
        return WebConfig()
    fetch_raw = raw.get("fetch")
    return WebConfig(fetch=_build_web_fetch_config(fetch_raw))


# ── multimodal: media-size gate for image/audio/etc. (#364 cluster) ─────────


_MULTIMODAL_ON_OVERSIZE = ("ask", "allow", "deny")


@dataclass
class MultimodalConfig:
    """``multimodal:`` — controls how Reyn handles large binary content
    (currently images from web__fetch / file__read / MCP servers) and
    where multimodal artefacts live on disk.

    Fields:
        max_bytes:
            Decoded-payload byte cap before the gate fires. Default 5MB
            matches Anthropic's per-image API limit. Counts the BINARY size
            (= ``len(response.content)`` / ``len(file_bytes)``), not the
            base64-encoded shape.
        on_oversize:
            What to do when a piece of media exceeds ``max_bytes``:

            - ``ask`` (default): prompt the user via the intervention bus
              with size + source info; yes loads the media, no drops it.
            - ``allow``: silently accept; no prompt. Use when running
              non-interactively in a trusted pipeline.
            - ``deny``: silently reject; the op returns ``status="denied"``
              with no media data. Use in cost-sensitive contexts where
              over-limit content should never reach the LLM.
        media_dir:
            Project-relative directory for image binary storage (issue
            #383 PR-C / E-full Phase 3). Files are flat-named with a
            timestamp + chain-id + tool prefix so ``ls -la`` sorts
            chronologically. User-browseable and user-deleteable.
        tool_results_dir:
            Project-relative directory for text-y tool result dumps
            (= #385 PoC foundation). PR-C lands the writer alongside
            ``media_dir``; PR-D wires the consumer + preview.
        base_url:
            Optional canonical URL prefix for cross-host path_ref
            consumption (#385 β core impl sub-task 3b). When set
            (= e.g. ``"https://reyn.example.com"`` from a deployed
            ``reyn web`` instance), ``MediaStore.save_*`` augments the
            path_ref with a ``url`` field pointing at
            ``<base_url>/agents/<agent>/tool-results/<artifact>`` so
            cross-host consumers (= A2A peers, MCP clients, browsers)
            can fetch the body via the resources router. Unset → no
            ``url`` field minted, same-host fast-path only (= backward
            compat for legacy / CLI-only deployments).

    Issue #364 lands this config + the shared ``require_media_load`` gate;
    paths #365 (file__read binary) and #366 (user chat input image) reuse
    them. Issue #383 PR-C extends with the storage paths.
    """
    max_bytes: int = 5_000_000
    on_oversize: Literal["ask", "allow", "deny"] = "ask"
    media_dir: str = ".reyn/media"
    tool_results_dir: str = ".reyn/tool-results"
    base_url: str | None = None


def _build_multimodal_config(raw: object) -> MultimodalConfig:
    """Parse the ``multimodal:`` section. Unknown keys ignored, bad types
    fall back to defaults.
    """
    if not isinstance(raw, dict):
        return MultimodalConfig()
    max_bytes_raw = raw.get("max_bytes")
    try:
        max_bytes = int(max_bytes_raw) if max_bytes_raw is not None else 5_000_000
    except (TypeError, ValueError):
        max_bytes = 5_000_000
    if max_bytes < 0:
        max_bytes = 5_000_000
    on_oversize_raw = raw.get("on_oversize")
    on_oversize: Literal["ask", "allow", "deny"]
    if (
        isinstance(on_oversize_raw, str)
        and on_oversize_raw in _MULTIMODAL_ON_OVERSIZE
    ):
        on_oversize = on_oversize_raw  # type: ignore[assignment]
    else:
        on_oversize = "ask"
    media_dir_raw = raw.get("media_dir")
    media_dir = (
        str(media_dir_raw) if isinstance(media_dir_raw, str) and media_dir_raw
        else ".reyn/media"
    )
    tool_results_dir_raw = raw.get("tool_results_dir")
    tool_results_dir = (
        str(tool_results_dir_raw)
        if isinstance(tool_results_dir_raw, str) and tool_results_dir_raw
        else ".reyn/tool-results"
    )
    base_url_raw = raw.get("base_url")
    base_url: str | None = (
        str(base_url_raw).rstrip("/")
        if isinstance(base_url_raw, str) and base_url_raw
        else None
    )
    return MultimodalConfig(
        max_bytes=max_bytes, on_oversize=on_oversize,
        media_dir=media_dir, tool_results_dir=tool_results_dir,
        base_url=base_url,
    )


def _build_voice_config(raw: object) -> VoiceConfig:
    """Parse `voice:` block. Unknown keys are ignored; bad types fall back to defaults.

    ``language`` semantics:
      - omitted          → defaults.language (= "ja")
      - explicit string  → that ISO code
      - "" / null in YAML → ``None`` (= Whisper auto-detect)
    """
    defaults = VoiceConfig()
    if not isinstance(raw, dict):
        return defaults
    if "language" in raw:
        lang_raw = raw["language"]
        if lang_raw is None:
            lang: str | None = None
        elif isinstance(lang_raw, str):
            lang = lang_raw.strip() or None
        else:
            lang = defaults.language
    else:
        lang = defaults.language
    return VoiceConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        model=str(raw.get("model", defaults.model)),
        language=lang,
        device=str(raw.get("device", defaults.device)),
        compute_type=str(raw.get("compute_type", defaults.compute_type)),
        sample_rate=int(raw.get("sample_rate", defaults.sample_rate)),
        cpu_threads=int(raw.get("cpu_threads", defaults.cpu_threads)),
        num_workers=int(raw.get("num_workers", defaults.num_workers)),
        max_duration_s=float(raw.get("max_duration_s", defaults.max_duration_s)),
    )
