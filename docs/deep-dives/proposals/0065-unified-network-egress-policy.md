# 0065 — Unified proxy / SSL-verify policy for reyn-originated network egress

- **Status**: Proposed (awaiting owner review)
- **Date**: 2026-07-18
- **Arc**: surfaced from the sandbox network arc (#3030/#3059) + the plugin-model ADR-0064 (install-time dep fetch)
- **Deferred (named, not decided here)**: OTEL export cert unification; HuggingFace-hub / ddgs (web_search) third-party-lib egress (best-effort env materialisation only); per-destination proxy routing (one proxy for all destinations in v1).

> Design contract (repo rule: spec/design lives in a doc). "reyn does X today" claims are backed by the §2 grounding (verified on `origin/main`, file:line in §6).

## 1. Context & the problem

Corporate/enterprise deployment is where network config *always* trips people up: outbound traffic must go through a corporate forward-**proxy**, and corporate TLS interception means a custom **CA bundle** (or, transiently, `verify=false`) is needed or every HTTPS call fails. reyn today has **no single place** to set either — it has **six independent, non-unified proxy/SSL subsystems**, each with a different default, so an operator behind a corporate proxy cannot make reyn work by configuring one thing.

The most damaging instance: **the highest-volume egress class — LLM / embedding calls via litellm — silently ignores `HTTP_PROXY`/`HTTPS_PROXY` entirely** (litellm's aiohttp transport ships `aiohttp_trust_env = False` and reyn never flips it). So reyn's own model calls cannot leave a proxied corporate network at all, with no config surface to fix it.

The goal: **one reyn-level policy governs proxy + ssl-verify for every outbound path reyn initiates** — its own in-process HTTP clients *and* every subprocess it spawns — set once, applied everywhere, no egress reading ad-hoc env or library defaults independently.

## 2. Grounding — the six subsystems today (verified on `origin/main`)

| # | Egress class | Mechanism | Proxy today | SSL-verify today |
|---|---|---|---|---|
| 1 | **LLM / embedding** (highest volume) | litellm aiohttp transport | **IGNORED** (`aiohttp_trust_env=False`, never flipped) | env-driven (`SSL_VERIFY`→`litellm.ssl_verify`→`SSL_CERT_FILE`→True) |
| 2 | **web_fetch / RegistryClient** | httpx + `PinnedAsyncHTTPTransport` (SSRF-pin) | **ZERO by construction** (explicit `transport=` disables httpx env-proxy) | `web.fetch.ca_bundle`/`verify_ssl` → `get_ssl_verify()` chain (RegistryClient docstring says pass `verify=`; **no call site does** — real doc/code gap) |
| 3 | **remote MCP / OAuth / webhook** | plain `httpx.AsyncClient` (no override) | env-trusting (works by accident) | httpx default (system CA via certifi) |
| 4 | **OTEL export** | `requests.Session()` | requests env-proxy (on) | `OTEL_EXPORTER_OTLP_CERTIFICATE` (a *distinct* env var) |
| 5 | **local-embedding download** | huggingface_hub (`requests`) | HF's own env vars | HF's own env vars |
| 6 | **web_search** | `ddgs` (DDGS) third-party lib | none passed by reyn | none passed by reyn |
| — | **git-clone** (skill/pipeline install source) | subprocess, sandbox `env_passthrough` | **forwards** `HTTP(S)_PROXY`/`NO_PROXY`/`SSL_CERT_*` | forwards |
| — | **uvx / npx** (stdio-MCP first-run fetch) + **uv dep fetch** (ADR-0064) | subprocess, sandbox clean env | **NOT forwarded** (only `PATH` + operator's `env:` block) | not forwarded |

**Consequences of the current mess** (each a §6 file:line): no centralized HTTP-client factory (~11 independent `httpx.Client` constructions); no reyn-owned `ssl.SSLContext` builder; `trust_env` never explicitly set anywhere; git-clone forwards proxy/CA to its child but the uvx/npx MCP-launch path does **not** (inconsistent sibling); five different SSL-verify resolution chains.

## 3. Decision

**Design principle (owner directive): UX-first by default; *tightening* security is the opt-in.** The default — `network:` absent — **honours the standard env proxy/CA (`HTTP(S)_PROXY`/`NO_PROXY`, `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/…) consistently across EVERY egress**, so a corporate setup works out of the box with the same env vars every other tool already reads — no `reyn.yaml` needed. (This is the fix: today litellm ignores the proxy and the SSRF-pin structurally blocks it, so the "just works from env" expectation silently fails.) The `network:` block is for **opt-in overrides — chiefly *tightening*** (pin one specific CA and ignore env, stricter SSRF, refuse proxy) — plus the one explicit, audited *loosening* escape `ssl_verify: false` (§3.7). Honouring env proxy/CA is not itself a loosening (`verify` stays true; proxy is routing; env CA only *adds* operator-set trust anchors); the only security-lowering field is the explicit, audited `ssl_verify: false`. reyn never silently overrides — config is the opt-in, in either direction.

### 3.1 One config surface — `reyn.yaml network:` (the SSoT)

```yaml
network:
  proxy:      "http://corp-proxy:8080"   # forward proxy for http+https (optional)
  no_proxy:   "localhost,127.0.0.1,.corp.internal"
  ssl_verify: true | false | "/etc/pki/corp-ca-bundle.pem"   # true=system CA, path=custom CA, false=insecure (see §3.7)
```

- Lives in `reyn.yaml` (the OUT-set — restart-scoped, structurally write-gated, like the sandbox block), because it is host/infrastructure config the operator owns, not a per-op field. Scope resolution mirrors existing config (project `.reyn/` merged onto user `~/.reyn/`), so a corporate default can live in `~/.reyn` and a project can override.
- Default (no `network:` block) = **UX-first**: reyn honours the standard env proxy/CA **uniformly across all egress** (§3.3–§3.5). A corporate operator sets the usual `HTTP(S)_PROXY`/`SSL_CERT_FILE` and everything reyn launches picks them up. `network:` is only needed to *override* — tighten (a pinned CA superseding env) or the audited `ssl_verify: false` loosen.
- **Clean break, no backward-compat (owner directive: no tolerated debt).** The unified resolver/factory/materialiser (§3.2–§3.5) **replaces** all six fragmented subsystems outright — litellm's `aiohttp_trust_env=False` blind spot, the SSRF-pin's env-proxy block, the ~11 ad-hoc `httpx.Client` constructions, and the five separate SSL-verify chains are **removed**, not shimmed. There is no "keep the old per-egress behaviour when `network:` is absent" fallback: the unified default (honour env everywhere) IS the behaviour. The old paths were inconsistent/broken (litellm proxy-blind), so replacing them is a strict correctness gain, not a compat risk.

### 3.2 One resolver — `resolve_network_policy()`

A single reyn-owned function resolves `(proxy, no_proxy, ssl_verify)` from `reyn.yaml network:` with env as the fallback (`HTTP(S)_PROXY`/`NO_PROXY`, `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`SSL_VERIFY`). This is the ONE place resolution lives; every seam below consults it. Replaces the five ad-hoc chains with one.

### 3.3 One in-process HTTP-client factory

A shared `build_http_client(policy, *, transport=None)` that every reyn-authored httpx client goes through (replacing the ~11 independent constructions), applying `proxy=` + `verify=` from the policy. web_fetch / RegistryClient / remote-MCP / OAuth / webhook all route through it. **RegistryClient's doc/code gap (§2 row 2) closes for free** — the factory always applies the resolved verify.

### 3.4 litellm — flip the proxy blind spot

For LLM/embedding (subsystem 1), reyn sets litellm's transport to honour the policy: pass the resolved proxy + `ssl_verify` into the litellm call path (via `litellm.aiohttp_trust_env`/an explicit client), so the highest-volume egress stops ignoring the corporate proxy. This is the single highest-impact fix in the ADR.

### 3.5 One subprocess-env materialiser — `network_env(policy)`

A single function turns the policy into the **standard proxy/CA env dict** (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` + lowercase aliases, `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`/`NODE_EXTRA_CA_CERTS`/`PIP_CERT`), injected into **every sandbox child** at spawn. This:
- **Unifies git-clone (which forwards) and uvx/npx + uv-dep-fetch (which don't)** under one mechanism — no more per-server hand-listing of proxy/CA in `env_passthrough`.
- **Structurally dissolves the env_passthrough papercut**: reyn *owns* the proxy/CA and pushes the standard var set; the operator configures it once in `reyn.yaml network:`, never per-server. The CA cert **file** is already readable (broad-read floor); only the env pointing at it was missing.
- Is a **curated, known, non-secret var set** — it does NOT relax the clean-env allowlist model into "pass everything"; secrets stay denied.
- Directly serves **ADR-0064 install-time dep fetch** (`uv venv`/`uv pip install` behind a corporate proxy) — the ADR-0064 §3.2 "network available at install time" only holds if that fetch gets proxy+CA, which is exactly this.

### 3.6 SSRF-pin × corporate proxy (crux 1)

web_fetch/RegistryClient carry `PinnedAsyncHTTPTransport` (DNS-rebind / SSRF protection: reyn resolves the target host itself and pins the connection to that IP, so a hostname that re-resolves to an internal address between check and connect can't be reached). A forward proxy changes the topology: **the proxy does the final name resolution and connection**, so reyn cannot pin an ultimate target it never resolves — the pin and a proxy are structurally exclusive on the same request.

**Decision (UX-first, tightening opt-in)**: when a proxy is honoured (the operator's env/config sets one), reyn pins/validates **the proxy endpoint** (the single trusted egress boundary) and DNS-rebind protection of the final target is delegated to the corporate proxy — which is precisely the egress-policy enforcement point the operator chose. When no proxy is present, the pin stays fully active on the target. The **opt-in tightening** for a security-strict operator is a `network:` field (e.g. `ssrf_pin: strict`) that **refuses to proxy** and keeps reyn's own target-pinning — trading corporate-proxy compatibility for reyn-enforced DNS-rebind protection. Default favours UX (proxy works); strict pinning is the opt-in. (Full owner-facing explanation of the mechanism accompanies this ADR.)

### 3.7 `ssl_verify: false` — supported, but loud + audited (crux 2)

**Decision**: support `ssl_verify: false` (needed for genuinely broken/self-signed internal endpoints during corporate setup — forbidding it pushes operators to worse workarounds like disabling the sandbox or editing the system trust store), but it must **never be silent**: a one-time WARN + a `network_ssl_verify_disabled` **audit-event (P6)** on every affected call, so `reyn events` records the downgrade. The **documented recommended path is a custom CA bundle** (`ssl_verify: /path`); `false` is the explicit escape hatch. Matches reyn's fail-visible / never-silently-loosen posture (Security band).

### 3.8 The unified rule (verbalised, one sentence)

> **reyn declares proxy + ssl-verify once in `reyn.yaml network:`; every outbound path reyn initiates — its own HTTP clients via one shared factory, and every subprocess it spawns via one materialised standard proxy/CA env — derives proxy + CA from that single policy, and no egress reads ad-hoc env or library defaults on its own.**

### 3.9 Scope: v1 core vs deferred (crux 3)

- **v1 (agent-facing egress, all in-scope)**: litellm LLM/embedding (§3.4), reyn's own httpx (web_fetch/RegistryClient/remote-MCP/OAuth/webhook via §3.3), and all subprocess network (git-clone unify + uvx/npx + ADR-0064 uv-dep-fetch via §3.5).
- **Deferred (best-effort or lower priority)**: OTEL export (opt-in telemetry, its own `OTEL_EXPORTER_OTLP_CERTIFICATE` — `network_env` can supply proxy but the cert var is separate; note, don't block); HuggingFace-hub local-embedding download + `ddgs` web_search (third-party libs with their own env-driven stacks — `network_env` materialisation reaches them best-effort via the standard vars, no reyn client to override); dogfood publish / REPL loopback (dev / no external boundary — out of scope).

## 4. Consequences

- **Corporate deployment works by setting one block** — `reyn.yaml network:` — instead of hand-configuring six subsystems (and being unable to fix litellm proxy at all).
- **The highest-volume egress (LLM/embedding) honours the corporate proxy** for the first time.
- **The sandbox env_passthrough papercut dissolves structurally** — reyn owns + pushes proxy/CA to every child; ADR-0064's install-time dep fetch works behind a proxy.
- **One HTTP-client factory + one resolver + one env-materialiser** replace ~11 ad-hoc client constructions and five SSL chains — a real reduction, and the RegistryClient doc/code gap closes.
- **Additive / no regression**: absent `network:`, every egress keeps today's env behaviour.
- **A deliberate, documented SSRF-pin relocation** under a configured proxy (§3.6) — the one security trade-off, surfaced for ratification.

## 5. Rejected alternatives

- **Per-server `env_passthrough` for proxy/CA (status quo)** — operator hand-lists proxy/CA env names in every MCP server's `env:` block; the papercut this ADR removes.
- **Auto-`trust_env=True` everywhere with no config surface** — rejected: `trust_env` leaks *all* env, undermining the clean-env allowlist; and it can't fix litellm (aiohttp default off) or the SSRF-pinned clients (explicit transport disables it) anyway.
- **Forbid `ssl_verify:false` entirely (custom-CA only)** — rejected: pushes operators to disable the sandbox or edit the system trust store; the audited escape hatch is safer than the workarounds it forces.
- **Per-destination proxy routing in v1** — deferred: one proxy for all destinations covers the corporate case; per-destination is complexity without a named need yet.

## 6. Appendix — grounding evidence (verified on `origin/main`, 2026-07-18)

- litellm chokepoints: `llm.py:1714` (acompletion), `llm.py:1340` (Router), `data/embedding/litellm_provider.py:480-482` (aembedding); proxy off = `litellm.aiohttp_trust_env=False` (litellm `__init__.py:477`), zero `aiohttp_trust_env`/`AIOHTTP_TRUST_ENV` hits in `src/reyn`.
- SSRF-pin clients: `op_runtime/web.py:295-299` (web_fetch), `core/registry/client.py:175-180` (RegistryClient); `_ssrf_pin.py`; httpx `allow_env_proxies = trust_env and transport is None` (`httpx/_client.py:685,1399`).
- SSL resolution: `get_ssl_verify()` consulted only at `registry/client.py:166`, `op_runtime/web.py:206`; `web._resolve_ssl_verify` (`web.py:190-217`).
- Subprocess env: `skill_install.py:268-273` (git-clone forwards proxy/CA), `mcp/client.py:1364-1446` (`_build_mcp_sandbox_policy`, no env_passthrough), `security/sandbox/policy.py:99,144` (`env_passthrough=[]`, `DEFAULT_SANDBOX_NETWORK=True`), sandbox child env allowlist `backends/landlock.py:377-383` / `seatbelt.py:248-254` / `noop_backend.py:48`.
- Remote MCP: `mcp/client.py:1608-1669` (`_open_http`/`_open_sse`, never passes `verify=`), fastmcp `create_mcp_http_client`.
- Other stacks: OTEL `observability/otel_exporter.py:537+` (`requests.Session`, `OTEL_EXPORTER_OTLP_CERTIFICATE`), OAuth `security/secrets/oauth.py:290,635` (plain httpx), webhook `interfaces/web/notifications.py:117`, HF `data/embedding/sentence_transformers_provider.py:342`, web_search `tools/search_backends/duckduckgo.py:9,16` (`ddgs`).
- No `import requests` in `src/reyn`; no centralized client factory; no reyn `ssl.SSLContext` builder.
