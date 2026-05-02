# Action and Channel Naming

Layer 0 reserves a tiny prefix; everything else is the schema's responsibility.
This document fixes the naming rules that all schemas follow so that:

- A reader can see at a glance whether a name is OpenUI-reserved or
  schema-defined.
- Names from one schema cannot collide with names from a future schema.
- Tooling (linters, validators, IDE completion) can reason about names
  without the schema being loaded.

---

## 1. Shape of a name

Every action and channel name is a **dot-separated, lower-snake-case
identifier**:

```
<namespace>.<verb-or-event>
<namespace>.<sub-namespace>.<verb-or-event>
```

Examples:

| Kind | Name | Meaning |
|---|---|---|
| action | `agent.submit` | submit a chat message |
| action | `agent.intervention.answer` | answer an intervention prompt |
| channel | `agent.message` | a chat message arrived |
| channel | `state.delta` | incremental state update |

Rules:

1. Lowercase ASCII letters, digits, dots, and `_` only. No hyphens, no
   slashes, no UTF-8.
2. Each dot-separated segment matches `[a-z][a-z0-9_]*`.
3. The first segment is the **namespace** (≈ domain noun: `agent`,
   `data`, `state`, `run`, `phase`, `permission`, …).
4. The last segment is the **verb** (for actions) or **event name**
   (for channels).
5. Optional middle segments group related names within a namespace.

---

## 2. Reserved namespaces

The following namespaces are **reserved by Layer 0** and Layer 1 schemas
MUST NOT define their own actions or channels under them, except as
specified.

### 2.1 `data.*` (Layer 0 reserved)

- `data.refetch` — action, defined in Layer 0 § 4.1.

Schemas MAY add:
- `data.<resource>.refetch` — a scoped refetch (e.g. `data.agents.refetch`)
  is allowed, since the segment after `data` is the resource.

Schemas MUST NOT redefine `data.refetch` itself.

### 2.2 `openui.*` (reserved for future Layer 0 use)

Reserved for future protocol extensions. Schemas MUST NOT define names
under this namespace.

### 2.3 No other Layer 0 reservations

All other namespaces (`agent`, `state`, `run`, `phase`, `permission`,
`budget`, etc.) are owned by the schema that defines them.

---

## 3. Namespace conventions

Schemas SHOULD adopt these conventions for readability and tooling:

### 3.1 One namespace per Layer 1 concept

Map each top-level concept in the schema to its own namespace. For Reyn:

| Reyn concept | Namespace |
|---|---|
| Agent | `agent` |
| Skill run | `run`, `skill` |
| Phase within a run | `phase` |
| Workspace state | `state`, `workspace` |
| Permission decisions | `permission` |
| Budget | `budget` |
| Topology (agent-to-agent links) | `topology` |
| Memory | `memory` |

### 3.2 Verbs vs. events

- Action names use **imperative verbs**: `submit`, `cancel`, `refetch`,
  `add`, `remove`, `update`, `answer`.
- Channel names use **past-tense or noun events**: `message`, `started`,
  `finished`, `updated`, `delta`.

```
action  agent.submit          (do this)
action  agent.cancel          (do this)
channel agent.message         (this happened)
channel run.started           (this happened)
channel run.finished          (this happened)
channel state.delta           (this update arrived)
```

### 3.3 Lifecycle events

When a concept has a lifecycle, prefer:

```
channel <concept>.started
channel <concept>.finished
channel <concept>.cancelled       (optional)
channel <concept>.failed          (optional)
```

(This pattern is borrowed from AG-UI's RunStarted / RunFinished and is
consistent across DAP, LSP, OpenTelemetry and similar protocols.)

### 3.4 Incremental updates

For incremental state updates, use a single `<resource>.delta` channel
carrying RFC 6902 JSON Patch:

```
channel state.delta            { patch: JsonPatch }
channel agents.delta           (if an agent-specific delta is useful)
```

The patch always includes its target path, so one channel can carry many
fine-grained updates.

---

## 4. Component names

Component names (in `manifest.components`) follow different rules from
actions and channels:

1. **PascalCase** identifiers: `TodayScreen`, `Conversation`,
   `SkillGraphPage`.
2. Match `^[A-Z][A-Za-z0-9]*$`.
3. Names are unique within a schema.
4. SHOULD reflect the surface (App / Studio / shared); page-level
   components SHOULD end in `Page` for Studio, `Screen` for App, or
   the bare component name for shared (`ChatMessage`, `AgentCard`).

---

## 5. Anti-patterns

The following SHOULD NOT appear in a Layer 1 schema:

| Anti-pattern | Why | Replace with |
|---|---|---|
| `submit`, `message` (no namespace) | collisions across concepts | `agent.submit`, `agent.message` |
| `agent_submit` (snake_case top level) | hard to group, no namespace tooling | `agent.submit` |
| `Agent.Submit` (PascalCase) | reserved for component names | `agent.submit` |
| `data.fetch` (matches reserved) | conflicts with Layer 0 `data.refetch` | `data.<resource>.refetch` |
| `openui.something` (reserved namespace) | reserved for future Layer 0 | pick a different namespace |
| `agents.submit` (plural namespace) | inconsistent with singular convention | `agent.submit` |

---

## 6. Versioning name changes

When a schema bumps to a new minor:

- **Adding** a new action / channel / component: free.
- **Renaming** an existing one: this is a breaking change → bump major.
- **Adding** an optional payload field to an existing action: minor.
- **Removing** a payload field: breaking → major.
- **Adding** an optional channel event field: minor.

Schemas SHOULD avoid renames; if a name was wrong, deprecate it (keep it
working, mark in description) and add the new one in parallel for a
deprecation period.

---

## 7. Examples — putting it together

```yaml
actions:
  agent.submit:               # ✅ reserved domain owner
  agent.intervention.answer:  # ✅ sub-namespaced under agent
  data.refetch:               # ✅ Layer 0 reserved, schema MUST include
  data.skills.refetch:        # ✅ scoped refetch, allowed under data.*
  run.cancel:                 # ✅ verb for run lifecycle action

channels:
  agent.message:              # ✅ past-tense event noun
  run.started:                # ✅ lifecycle past-tense
  run.finished:
  state.delta:                # ✅ JSON Patch convention
  budget.updated:
```

Schemas that follow these rules can be linted automatically (a future
`@openui/validator` CLI will check), and tooling such as IDE completion
can offer suggestions per namespace.
