# OpenUI Manifest Format

A **manifest** is the formal description of a Layer 1 schema. Every Layer 1
schema is expressed as a manifest plus accompanying type definitions; the
manifest tells hosts and designs what actions, channels, components, and
data shape exist for the domain.

This document specifies the manifest format. The first concrete manifest is
[reyn-ui/v1](../schemas/reyn-ui-v1/manifest.yaml).

---

## 1. File format

A manifest is a YAML or JSON file; both are accepted. YAML is preferred for
readability.

The canonical filename is `manifest.yaml` (or `.json`) at the root of a
schema directory:

```
docs/deep-dives/spec/openui/schemas/<schema-id>/
├── manifest.yaml         ← the manifest
├── data.types.ts         ← TypeScript types for OPENUI_DATA
├── components.md         ← component contracts (props per required component)
└── README.md             ← schema overview, versioning policy
```

A JSON Schema validator for manifests lives at
[../schemas/manifest.schema.json](../schemas/manifest.schema.json).

---

## 2. Manifest structure

```yaml
schema: "<domain>/<version>"        # identifier; required
title: "Reyn agent OS UI v1"        # human-readable; required
description: |                       # multi-line; required
  Free-text description of what apps in this domain are like and what
  this version's scope is.
spec_version: "1.0"                  # OpenUI Layer 0 version this targets

data:                                # required
  type: ReynUiData                   # references types in data.types.ts
  description: |                     # optional
    Free-text description of the top-level data shape.

actions:                             # required (may be empty {})
  agent.submit:
    description: |
      User submitted a chat message to an agent.
    payload:
      agentId: string
      text: string
    returns: void
  agent.intervention.answer:
    description: |
      User answered an intervention prompt.
    payload:
      choiceId?: string
      text?: string
    returns: void
  data.refetch:                      # always present per Layer 0 § 4.1
    description: Re-fetch initial data.
    returns: ReynUiData

channels:                            # required (may be empty {})
  agent.message:
    description: |
      Backend pushed a chat message to the conversation.
    event: ChatMessage
  state.delta:
    description: |
      Incremental state update, RFC 6902 JSON Patch.
    event: { patch: JsonPatch }
  run.started:
    description: |
      A skill run started.
    event: { runId: string, skillName: string, agentId: string }
  run.finished:
    description: |
      A skill run finished.
    event: { runId: string, status: "ok" | "failed" | "aborted" }

components:                          # required
  TodayScreen:
    surface: app                     # "app" | "studio" | "shared"
    required: true
    props:
      onPickAgent: (agentId: string) => void
      onOpenLibrary: () => void
      lang: "en" | "ja"
      layout?: "default" | "hero" | "agents-first"
  Conversation:
    surface: app
    required: true
    props:
      agentId: string
      onSubmit: (text: string) => void
      onAnswerIntervention: (a: { choiceId?: string, text?: string }) => void
      lang: "en" | "ja"
  # ... more components

extensions:                          # optional
  notes: |
    Studio components (SkillGraphPage, RunTimelinePage, PermissionsPage)
    expose Reyn-specific concepts (Skill, Phase, Workspace, Topology)
    that are passed through as opaque JSON values typed in data.types.ts.
```

---

## 3. Field reference

### 3.1 Top-level fields

| Field | Required | Type | Description |
|---|---|---|---|
| `schema` | ✅ | string | `<domain>/<version>` identifier. SemVer-compatible version. |
| `title` | ✅ | string | One-line human-readable title. |
| `description` | ✅ | string | Multi-paragraph description. |
| `spec_version` | ✅ | string | Layer 0 spec version targeted (e.g. `"1.0"`). |
| `data` | ✅ | object | See § 3.2. |
| `actions` | ✅ | object | See § 3.3. May be empty `{}`. |
| `channels` | ✅ | object | See § 3.4. May be empty `{}`. |
| `components` | ✅ | object | See § 3.5. |
| `extensions` | optional | object | Free-form notes / forward-compatibility hints. |

### 3.2 `data`

```yaml
data:
  type: <TypeName>
  description: |
    Free-text description.
```

`type` references a TypeScript interface or type alias in the
sibling `data.types.ts` file. The shape of `OPENUI_DATA` for designs
targeting this schema MUST conform.

### 3.3 `actions`

A map from action name to action descriptor. Action names follow
[action-channel-naming.md](action-channel-naming.md).

```yaml
actions:
  <namespace.verb>:
    description: |
      What this action does, when designs invoke it.
    payload:
      <field>: <type>      # repeatable
    returns: <type>        # use `void` when no return value
```

Types may reference TypeScript types from `data.types.ts`. Use TypeScript
union/literal syntax inline where possible (`"a" | "b"`, `string[]`).

### 3.4 `channels`

A map from channel name to channel descriptor. Channel names follow
[action-channel-naming.md](action-channel-naming.md).

```yaml
channels:
  <namespace.event-name>:
    description: |
      When this channel emits and what it carries.
    event: <type>          # shape of each event published on this channel
```

### 3.5 `components`

A map from component name to descriptor. Component names are PascalCase
identifiers; designs export functions or React components by these names.

```yaml
components:
  <ComponentName>:
    surface: "app" | "studio" | "shared"   # which face of the app
    required: true | false                  # must designs export this?
    props:
      <propName>: <type>
      <optionalProp>?: <type>
```

`surface` lets a host present designs that only ship the App face (and
fall back to a default for the Studio face, or hide Studio entirely).

`required` MAY be `false` for components that not every design needs.
Hosts that need them and find them missing fall back to a default
implementation or hide the corresponding feature.

### 3.6 `extensions`

A free-form object for schema authors to attach notes that are useful but
not load-bearing. Example uses:

- Pass-through type list (Reyn carrying Skill / Phase / Workspace as opaque
  JSON values inside Layer 1).
- Compatibility notes ("designs targeting reyn-ui/v1 also work with
  reyn-ui/v0.x because we kept additive fields").
- Migration guides between adjacent versions.

Hosts and designs MUST NOT rely on `extensions` for behaviour — anything
load-bearing belongs in the typed sections above.

---

## 4. Versioning

Schema identifiers use **SemVer**:

```
reyn-ui/1.0.0   ← initial
reyn-ui/1.1.0   ← additive: new component / channel / action / data field
reyn-ui/2.0.0   ← breaking: removed or changed existing
reyn-ui/1.1.1   ← clarification only
```

The shorter form `reyn-ui/v1` accepts any 1.x.y version (host-discretion
matching). The full form `reyn-ui/1.2.0` pins exactly. Designs declare
which they need:

```yaml
# in a design's design.yaml or similar
schema: reyn-ui/v1     # any 1.x.y compatible
schema: reyn-ui/1.2    # any 1.2.x
schema: reyn-ui/1.2.3  # exact
```

Hosts implement one or more concrete versions and accept compatible
design declarations.

---

## 5. Validation

A manifest can be validated structurally via the JSON Schema at
[../schemas/manifest.schema.json](../schemas/manifest.schema.json).
A future `@openui/validator` CLI will run that validation plus content
checks (referenced types exist, action / channel names follow naming
rules, etc.).

For now, validation is manual: human reviewers ensure the manifest
matches the spec, and the typed sections (`data.types.ts`,
`components.md`) are kept in sync with the manifest.

---

## 6. Example

See [reyn-ui/v1 manifest](../schemas/reyn-ui-v1/manifest.yaml) and the
sibling [data.types.ts](../schemas/reyn-ui-v1/data.types.ts) +
[components.md](../schemas/reyn-ui-v1/components.md) for a complete
working example.
