# Proposal 0058 — Web Surface Modularity (+ Chainlit Retire)

**Status:** OWNER-RATIFIED; **Phase 1-4 landed** — Phase 1 auth-gate (#2837), Phase 2 SurfaceSpec registry + secure-default A2A/MCP-OFF folding Phase 3 (#2849), Phase 4 chainlit clean-break retire (#2850). **Phase 5 (optional refactor fold) remains open**, not dispatched. **Origin:** owner-designed, architect-formalized, 2026-07-11.
**Scope:** the `reyn web` FastAPI gateway's HTTP surfaces. **Auth model:** single-user/single-token now; multi-user identity = future (out of scope).

## Owner ratification (2026-07-11) — decisions LOCKED
All open questions from the design co-vet are resolved; this section is the authoritative decision record. The design body below stands as written except where this block overrides.
- **[a] Secure-default line — CONFIRMED (my recommendation adopted).** ON = AG-UI / WebUI (OpenUI static) / REST `/api` (auth-gated) / `/health` / resources. **OPT-IN OFF = A2A + MCP** (the broad remote-integration ports). webhook = existing per-plugin opt-in. (§D3.)
- **[b] CLI shape — `--enable <surface>` / `--disable <surface>` per-surface toggle** (NOT a `--surfaces a,b` comma-allowlist). Precedence: **CLI `--enable/--disable` > `web.surfaces` config > secure-default.** (§D2.)
- **[c] Auth-layer — ALREADY LANDED.** The security-first phase (D1) merged as **PR #2837** (mount-front class-aware `AuthGateMiddleware`, closing the A2A/MCP/REST unauth gap). So the phasing below starts at the surface-registry phase; D1 is DONE, not pending.
- **[d] resources surface — ON** (part of the ON set; auth-gated by the landed D1; multi-class per §D1 "resources = any authenticated").
- **[e] Chainlit retire — same-arc, clean-break** (owner "retire"; §D5).
- **Concurrency:** runs in parallel with FP-0057 RAG (disjoint files: `interfaces/web/server` + `config` + `cli` here vs `data/index` + `op_runtime` there).

## Phasing (owner-ratified, D1 already landed)
- **~~Phase 1 — auth-layer~~ DONE (PR #2837, merged).** Class-aware `AuthGateMiddleware` front-of-mounts.
- **Phase 2 — surface registry + config + CLI (THIS dispatch).** §D4 `SurfaceSpec` registry (FP-0041 generalization) + §D2 `web.surfaces` config + `--enable/--disable` CLI + precedence. Gate: **reachability strip** — an enabled surface is reachable end-to-end, a disabled one 404s (strip the enabled-check → mounts anyway → RED).
- **Phase 3 — secure-default.** §D3 (A2A/MCP default-OFF). Gate: fresh install → exactly the ON set; A2A/MCP 404 until `--enable`d.
- **Phase 4 — chainlit retire.** §D5 clean-break.
- **Phase 5 — refactor fold [optional].** §D6 (#2 progress-bridge base / #3 resolve helper).

---

## 1. Motivation

`reyn web` mounts a set of HTTP surfaces (A2A, MCP, AG-UI, REST `/api`, openui browser, webhook plugins, resources, health). Today all core surfaces mount **unconditionally** — an operator who wants only a local browser UI still exposes the A2A peer port and the MCP tool-provider port. Owner-ratified goal: **make each surface config/CLI opt-in with a secure-minimal default, and make auth an orthogonal layer in front of all mounts** — so the exposed surface set is a deliberate, legible operator choice, least-exposure by default.

This proposal is built on a **wiring-coherence review** (§2) of how the surfaces are actually implemented, so the modularity seam is cut where the code already wants it (no reinvention) and so the review's biggest finding — a real security gap — is fixed as part of the same arc.

---

## 2. Wiring-coherence review (grounding — all four surfaces read primary)

**Genuinely SHARED substrate** (same object/function, not parallel copies):
- The `AgentRegistry` process singleton (`get_registry()`), read by every surface.
- `registry.resolve_session` — the session-resolution primitive all non-AG-UI surfaces wrap.
- `Session.answer_pending_intervention` / `answer_intervention_by_id` — the authoritative HITL resolution core. The peer path **hardcodes `external_source=True`** (session.py:5128) → peer answers are always fenced.
- `session._chat_events` (EventLog) — subscribed by the A2A/MCP progress bridges and AG-UI's `_SessionFrameSource` alike.

**REINVENTED — refactor candidates:**
1. **[SECURITY-CRITICAL] Auth is surface-local, not orthogonal.** Only AG-UI enforces the P0 auth context — `authenticate_request` (endpoint.py:105) is called in every AG-UI handler (events :256-257, seize :366-367, submit :395-396, `authorize_write` :319/:427). **A2A, MCP, REST `/api/*`, and resources have ZERO auth-gate calls** (verified per-handler, not grep); the only middleware is CORS (server.py:286). All non-AG-UI surfaces are unauthenticated on **all** binds. The loopback default protects the common case, but a **non-loopback bind (which ADR-0039's posture supports with TLS+token for AG-UI) exposes every non-AG-UI surface unauthenticated** — a TLS-passing client acts without the token AG-UI requires. Two distinct severities, do not conflate:
   - **A2A/MCP** — unauthenticated turn-driving + intervention-answer injection, but the answer is **always `external_source=True` fenced** (session.py:5128), so blast-radius is *bounded* ("unauthenticated access to a fenced-peer surface").
   - **REST `/api/*` control-plane — unauthenticated AND UNFENCED raw mutations, the sharper severity** (the fence only covers conversation-injection, not REST ops, verified primary): `DELETE /permissions` (revoke approval entries, permissions.py:78/:94) + `GET /permissions` (disclose the approval store), `PATCH /budget/caps` (budget.py:99 — **raising caps defeats the cost/budget bounding band → unbounded spend**), `POST`/`DELETE /agents` (agents.py:66/:103) and `/topologies` (create/delete). These are direct control-plane privilege/state mutations with no fence.
   **This is owner requirement #4's gap made concrete — and it is broader/sharper than "A2A refactor room": the REST control-plane is the priority.**
2. **[modest dup] Progress-bridge scaffold.** `_A2AProgressBridge` and `_MCPProgressBridge` are two separately-written classes both doing "subscribe `session._chat_events` → forward to a protocol sink" (they mirror each other by comment, not shared code).
3. **[minor dup] `resolve_session` wrapped 3×** (`resolve_a2a_session` / `resolve_mcp_session` / `resolve_webhook_session`) — thin per-transport wrappers.
4. **[modularity gap] Mount-conditionality reinvented.** Webhook *plugins* already opt-in via FP-0041 (`register_router(config) -> APIRouter | None`, None = graceful skip, + `webhooks.yaml` activation, + entry-point discovery). Core surfaces bypass this with unconditional `app.include_router`. This inconsistency *is* the thing to generalize.

**LEGITIMATELY SEPARATE — verified protocol semantics, NOT refactor targets** (excluded from any common-ization):
- **Wire encoding.** A2A JSON-RPC 2.0, MCP SDK `TextContent`, webhook third-party SDKs, AG-UI `Frame` codec are genuinely different protocols. A common `Frame` intermediate would be *wrong* — only AG-UI is a display-streaming UI surface.
- **Attach / SurfaceManager / active-driver / fail-close.** These are UI-operator concepts (one interactive driver, seize, grace window). A2A/MCP are stateless request/response (per-context/per-run session) and correctly don't need them.
- **Reply-delivery model.** A2A/MCP harvest the reply from `session.history` and return it as a JSON-RPC / tool result (request/response); AG-UI streams display frames. So A2A/MCP *correctly* do not subscribe `outbox_hub` for the reply — a legitimate protocol difference, not a hub-adoption gap. Only the progress side-channel is the #2 dup.

---

## 3. Design

### D1 — Auth-layer consolidation [PHASE 1, security-first]
**Framing: reuse the P0 auth substrate, add enforcement at the mount front. No new auth is built.** The `auth/` package (`AuthContext` core + per-OS peer-cred + TLS, ADR-0039 P0) is already centralized; the gap is only that *enforcement* is surface-local (AG-UI-only). The fix hoists enforcement to a common front-of-all-mounts layer.

- **Mechanism:** an ASGI middleware (front of every mount, so it is truly orthogonal — not a per-router `Depends` that each surface must remember) that resolves `ConnectionIdentity` via the existing `AuthContext.authenticate` seam and stamps `request.state.identity` (+ identity **class**). Behind it, a **per-surface policy table** decides what each surface requires.
- **Identity-CLASS-aware (ADR-0039 keystone preserved — uniform gate would be WRONG):** the layer assigns a class; downstream keeps its fencing. **Class is derived from the SURFACE (path prefix)** — `/agui`+`/api`+`/static` → operator (unfenced, `external_source=False`), `/a2a` → peer (fenced, `external_source=True`, already hardcoded at session.py:5128 — unchanged), `/mcp` → client. This surface-derived class is exactly what lets a *single* middleware be class-aware cleanly (the token authenticates *within* the class the path implies). The middleware **authenticates** (closes the "who are you" gap); the existing fencing **authorizes** blast-radius. A2A/MCP still fence; they just stop being anonymous.
- **Per-surface policy has three shapes, not just "gate/don't":** (i) **common-gate** (AG-UI/REST/A2A/MCP/resources — authenticate via the P0 seam in the surface's class); (ii) **surface-native auth pass-through** (webhook plugins verify their own HMAC signing-secret, gateway/sample_*/webhook.py — the common gate must **not** double-gate them, it delegates); (iii) **open** (`/health` AND the openui shell assets `/static`+`/`+`/web/designs`). The policy table encodes which surface is which. **openui shell = OPEN, not operator-gated:** the browser loads `/static/index.html` + CSS/JS *before/without* the token in the URL query (relative asset requests don't carry `?token=`), so gating them would break shell loading on a non-loopback bind; the shell is non-sensitive (no embedded secret — the operator supplies the token at runtime), and the sensitive operations it *calls* (`/agui` SSE+POST, `/api`) ARE gated. Standard public-assets / gated-API split.
- **`resources` is multi-class — its policy accepts ANY authenticated identity, not operator-only.** It is a cross-host `path_ref` content fetch consumed by browser (operator) AND A2A peers (fenced), and is already content-safe (agent-existence + path-traversal validated per its docstring). Gating it operator-only would break peer `path_ref` fetch — so its policy is "any authenticated class."
- **Loopback/non-loopback parity:** the CLI already fail-closes the server *start* on non-loopback without a token (`_apply_auth_startup`, transport-level) — but only AG-UI *enforces* that token per-request today. D1 adds per-request **enforcement** on every surface, so on non-loopback the token is required uniformly, not AG-UI-only.
- **AG-UI unchanged in behavior** (it already gates); its per-handler calls become redundant with the middleware but can stay (defense-in-depth) or be simplified — a build-time detail, not a behavior change.
- **Standalone-PR option:** because this closes a live (bounded) security gap independent of modularity, it can land as a **standalone security PR before** the rest of the arc. Recommend surfacing this to the owner as a risk-timing choice (close the gap early vs bundle with modularity).

### D2 — Surface enablement (config + CLI opt-in)
- **Config:** add `web.surfaces` to `WebConfig` (config/media.py:114, alongside `fetch`/`ws_max_size`/`auth`) — a `SurfacesConfig` dataclass with a per-surface `enabled: bool`, plus a `_build_surfaces_config` loader (mirrors the existing `_build_web_fetch_config` pattern). Operator-owned by the same model as `SandboxConfig`/`AuthConfig`.
- **CLI:** `reyn web` (cli/commands/web.py) gains surface control via **`--enable <surface>` / `--disable <surface>` per-surface toggle** (owner-decided, §[b] — not a `--surfaces a,b,c` comma-allowlist), a delta over config/default. Propagated to the server process by the established env-var channel (`REYN_WEB_*`, like `REYN_WEB_DEFAULT_DESIGN`).
- **Precedence:** CLI flag **>** `web.surfaces` config **>** secure-default (§D3). CLI overrides config, as owner specified.
- **Operator-owned / LLM-untouchable (protect-at-use, permission-model.md:40):** surface enablement is read only at operator-driven `reyn web` **launch** — it is never on an LLM op path (the LLM cannot launch or reconfigure a running gateway), and CLI overrides config at each launch. So it is inherently operator-owned; no config-write carve-out is strictly required (defense-in-depth optional). State this explicitly.

### D3 — Secure-minimal default (least-exposure, grounded per-surface)
Default the exposed set to the **common `reyn web` purpose = a local operator UI**, and make the broad network-integration ports **opt-in**:

| Surface | Default | Grounding |
|---|---|---|
| AG-UI `/agui/*` | **ON** | The UI purpose of `reyn web`; already auth-gated; local-first. |
| openui `/static`, `/`, `/web/designs/*` | **ON** | The browser shell — the local UI. |
| REST `/api/*` | **ON** | The UI's control-plane (agents/permissions/budget). ON, **but now auth-gated by D1** (closes its current unauth exposure). |
| `/health` | **ON** | Trivial meta, no session access. |
| resources `/agents/.../tool-results/*` | **ON-when-any-consumer-on** | A content-fetch dependency of browser (present-node assets) AND A2A/MCP (`path_ref`). Not an independent surface — gate it on "any consuming surface enabled" (§5 open-q d). Auth-gated by D1. |
| A2A `/a2a/*` | **OPT-IN (OFF)** | A broad **remote peer-integration** port. You enable it when you want reyn to be an addressable A2A peer. Off by default = least-exposure + defense-in-depth for the D1 gap. |
| MCP `/mcp/*` | **OPT-IN (OFF)** | A broad **tool-provider** port for outer LLM clients. Enable when you want reyn to be an MCP server. Same rationale. |
| webhook plugins | **already opt-in** (per-plugin `enabled`) | Unchanged; folds into the surface registry (§D4). |

**OSS grounding:** Prometheus ships `--web.enable-admin-api` / `--web.enable-lifecycle` **default-OFF, opt-in** for its broad/dangerous surfaces; Kubernetes `--runtime-config` enables APIs per-need; Jupyter server extensions are explicitly enable/disable. **Charter grounding:** Security = least-exposure (secure-default); Product Think = legible, selectable (config+CLI); System Design = composition-root config (the surface registry lives at the server composition root, §D4, not scattered `include_router` calls).

### D4 — Conditional-mount generalization (FP-0041, no-reinvention) [resolves finding #4]
Generalize the existing plugin opt-in into a single **surface registry** at the server composition root. Each surface (core + plugin) declares a mount-spec:
```
SurfaceSpec(name, mount(app, config) -> APIRouter | None, default_enabled, identity_policy)
```
`server.py` iterates the registry and mounts a surface iff it is enabled (CLI > config > `default_enabled`) and `mount(...)` returns a router (None = graceful skip, exactly the FP-0041 `register_router` contract — e.g. MCP's `[mcp]`-extra-absent case becomes a natural None). This unifies the plugin path and the core path onto **one** conditional-mount seam — the thing that is reinvented today.

### D5 — Chainlit retire (clean-break, folded) [owner tech-debt call]
Retire the parallel chainlit PoC browser UI (superseded by openui + AG-UI). Verified **clean-break safe**: zero external production importers; it is a standalone `chainlit run` subprocess (**not mounted in `server.py`** → the surface registry is untouched by it). Footprint to delete in one PR: the `chainlit_app/` package (13 modules + assets), `cli/commands/chainlit.py` + its 2-line registry entry (`commands/__init__.py:12,32`), the `[chainlit]` extra (`pyproject.toml:102`) + package-data glob (`:188`), **17 test files** (correcting the "~4" estimate), and doc references (user-facing: `feature-map.md:603` row + the `reyn-yaml.md/.ja` "TUI + chainlit" mentions — purge; the historical ADR/proposal refs stay). Also remove the now-dead `NullPresentationConsumer("chainlit")` ratchet assertion (`test_present_sink_na_ratchet_2708.py:35`) since "chainlit" leaves `_NA_PRESENTATION_SURFACES`.

### D6 — Refactor candidates (build-time fold, from §2) [optional]
- **#2** shared `ProgressBridge` base (subscribe `_chat_events` → pluggable protocol sink) — fold if a phase touches the A2A/MCP bridges; sinks stay surface-local.
- **#3** one parametrized `resolve_session` helper — low value, optional.
- **DO NOT touch** the legitimately-separate seams (wire encoding, attach/SurfaceManager, reply-delivery) — call this out explicitly so a future "consolidation" doesn't over-reach.

---

## 4. Phased build
1. **Phase 1 — auth-layer consolidation [SECURITY-FIRST].** D1: class-aware middleware front-of-mounts + per-surface policy; close the unauth gap on **every** non-AG-UI surface, **REST `/api` control-plane prioritized** (unfenced mutations); AG-UI behavior unchanged; A2A-peer fencing preserved. **Standalone-PR-able** (owner risk-timing call). *Gate (strip-falsify each):* on a non-loopback bind without a token — the **unfenced `/api` mutations fail closed** (`DELETE /permissions`, `PATCH /budget/caps`, `POST`/`DELETE /agents`+`/topologies` → 401, not 204/200); A2A/MCP require identity; strip the middleware → any of these reachable unauthenticated → RED. A2A-answer `external_source=True` preserved (fencing not weakened by adding auth).
2. **Phase 2 — surface registry + conditional-mount generalization.** D4 + D2 (config `web.surfaces` + CLI + precedence). *Gate:* a disabled surface returns 404 and mounts nothing; strip the enabled-check → surface mounts anyway → RED; CLI > config precedence test.
3. **Phase 3 — secure-default line.** D3 (A2A/MCP default-OFF). *Gate:* a fresh install with no config exposes exactly the ON set; A2A/MCP are 404 until enabled.
4. **Phase 4 — chainlit retire.** D5 clean-break. *Gate:* `grep chainlit src/` = 0; tests deleted; doc purge (current-facing only); dead ratchet assertion removed.
5. **Phase 5 — refactor fold [optional].** D6 #2/#3 if the earlier phases touched those files.

Ordering rationale: the security gap is real and owner-risk-relevant → first. Modularity mechanism → second. Default policy (a one-line change once the mechanism exists) → third. Cleanup → fourth. Optional dedup → last.

## 5. Questions raised during design — all resolved (see "Owner ratification" above)
- **(a) Secure-default line:** ✅ resolved — **A2A/MCP default-OFF** (least-exposure), everything else ON.
- **(b) CLI shape:** ✅ resolved — **`--enable <s>` / `--disable <s>` per-surface toggle**, not a `--surfaces a,b` comma-allowlist.
- **(c) Auth-layer timing:** ✅ resolved — landed as a **standalone security PR** (D1, PR #2837, already merged).
- **(d) resources surface:** ✅ resolved — **"ON-when-any-consumer-on"**.
- **(e) Chainlit retire:** ✅ resolved — **same-arc, clean-break** (no deprecation window).

## 6. Test plan (arc-level, beyond the per-phase gates)
- **Auth:** per-surface identity-required assertions on a non-loopback bind; identity-class fencing preserved (A2A peer stays fenced); strip-falsify each surface's gate.
- **Modularity:** enable/disable → mount vs 404; CLI > config precedence; the secure-default ON-set on a fresh install.
- **Reachability (COMPLETE-means-reachable):** an *enabled* surface is actually reachable end-to-end; a *disabled* one 404s — assert both, not just "the flag parses."
- **Chainlit:** retire completeness (src grep = 0, tests removed, current-doc purge).
- **Doc-surface (arc-closure gate):** a discoverable how-to for `--enable`/`--disable`; feature-map rows for the surface-modularity feature + the auth-layer; drift-purge of any "all surfaces always on" wording. *(Applying the doc-surface completeness gate from the ADR-0039 closure lesson.)*
