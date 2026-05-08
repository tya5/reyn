/**
 * reyn-ui/v1 — TypeScript types for the schema's Layer 1 data shape.
 *
 * `window.OPENUI_DATA` MUST conform to `ReynUiData` when
 * `window.OPENUI_SCHEMA === "reyn-ui/v1"`.
 *
 * Carries Reyn-specific concepts (Skill, Phase, Workspace, Run) as
 * structured JSON. The Layer 0 protocol does not interpret these types;
 * it only routes data of this shape between host and design.
 *
 * @packageDocumentation
 */

/**
 * Top-level shape of `window.OPENUI_DATA` for reyn-ui/v1.
 */
export interface ReynUiData {
  /** List of available agents. */
  AGENTS: Agent[];
  /** Recent humanized activity entries shown on the App "Today" screen. */
  RECAP: RecapEntry[];
  /** Suggested quick-start cards on Today. */
  QUICKSTARTS: Quickstart[];
  /** Library catalog of "things your agents can do" cards. */
  LIBRARY: LibraryItem[];
  /** App-side transcript fixture for the active conversation. */
  CONVO_ARIA: ChatMessage[];
  /** Studio-side transcript fixture (same conversation, engine-level annotations). */
  CONVO_ARIA_STUDIO: ChatMessage[];
  /** Resolved phase graph for the currently inspected skill. */
  SKILL_GRAPH: SkillGraph;
  /** Raw Markdown source of the currently inspected skill. */
  SKILL_MD: string;
  /** Event log for the currently inspected run (Studio Run timeline). */
  RUN_EVENTS: RunEvent[];
  /** List of recent runs for the Runs table. */
  RUNS_LIST: RunSummary[];
  /** Per-op permission rules table. */
  PERMISSIONS: PermissionRule[];
  /** UI copy strings keyed by language. */
  COPY: { en: I18nKeys; ja: I18nKeys };
}

// ── Agents ─────────────────────────────────────────────────────────────────

/**
 * Mascot identifier for AnimalAvatar.
 */
export type AgentAnimal = "fox" | "otter" | "crane" | "hare" | "owl";

export interface Agent {
  /** Stable identifier (slug). */
  id: string;
  /** Display name. */
  name: string;
  /** Mascot identifier. */
  animal: AgentAnimal;
  /** Hex color used for avatar background and accents. */
  color: string;
  /** One-word role label (e.g. "Researcher", "Writer"). */
  role: string;
  /** One-line "what I'm good at" description. */
  blurb: string;
  /** Free-text personality / system-prompt addendum. */
  role_prompt: string;
  /** Allowlist of skill ids this agent may invoke. */
  allowed_skills: string[];
  /** Humanized "last seen" string (e.g. "2 minutes ago"). */
  last_active: string;
  /** Humanized current activity (e.g. "researching renewable energy"). */
  activity: string;
}

// ── Today screen ───────────────────────────────────────────────────────────

export interface RecapEntry {
  /** Agent id (matches `Agent.id`). */
  agent: string;
  /** HTML-formatted recap line; `<b>` is honoured. */
  text: string;
  /** Humanized timestamp (e.g. "2m ago"). */
  time: string;
  /** Run id this recap derives from. */
  run: string;
}

export interface Quickstart {
  id: string;
  /** Icon name (see `Icon` component). */
  icon: string;
  title: string;
  /** One-line description. */
  sub: string;
}

// ── Library ────────────────────────────────────────────────────────────────

export interface LibraryItem {
  /** Maps to a backing skill id. */
  id: string;
  /** Verb-phrase title (e.g. "Research a topic"). */
  title: string;
  /** One-line description. */
  sub: string;
  /** Icon name. */
  icon: string;
  /** Hex accent color. */
  color: string;
  /** Agent name typically used for this card. */
  tag: string;
}

// ── Conversation ───────────────────────────────────────────────────────────

/**
 * Message kinds rendered on the App-side conversation.
 *
 * - `user`: user-typed message.
 * - `agent`: agent's reply (model output).
 * - `status_done`: humanized "task complete" banner with steps.
 * - `question`: agent's mid-task question (intervention prompt with chips).
 */
export type AppMessageKind = "user" | "agent" | "status_done" | "question";

/**
 * Message kinds rendered on the Studio-side conversation.
 *
 * The Studio surface deliberately excludes `status_done` and `question` —
 * those are humanized App-side renderings of underlying engine events,
 * which Studio shows verbatim in its event timeline instead.
 */
export type StudioMessageKind = "user" | "agent";

export interface ChatMessage {
  kind: AppMessageKind | StudioMessageKind;
  /** Message body. Required for `user` / `agent` / `question`. */
  text?: string;
  /** `status_done` banner title. */
  title?: string;
  /** `status_done` banner subtitle. */
  sub?: string;
  /** `status_done` humanized step list. */
  steps?: string[];
  /** `question` suggestion chips. */
  chips?: string[];
  /** Studio-side debug annotation (e.g. "phase: synthesize"). */
  meta?: string;
}

// ── Skill graph ────────────────────────────────────────────────────────────

/**
 * Special-role markers for phases.
 *
 * - `entry`: the skill's entry phase.
 * - `sub`: a sub-skill node (`@name` reference).
 * - `end`: the sentinel `end` finishing phase.
 * - `undefined`: a regular phase.
 */
export type PhaseKind = "entry" | "sub" | "end" | undefined;

export type PhaseStatus = "done" | "active" | "pending";

export interface SkillPhase {
  /** Phase identifier. */
  id: string;
  /** Special-role marker; absent for regular phases. */
  kind?: PhaseKind;
  /** Input artifact type name. */
  input: string;
  /** Free-text instructions / description. */
  desc: string;
  /** Times this phase has been entered in the current run. */
  visits: number;
  /** Current execution status. */
  status: PhaseStatus;
}

export interface SkillGraph {
  /** Skill name. */
  name: string;
  /** Resolved source path of the skill .md. */
  source: string;
  /** Entry phase id. */
  entry: string;
  /** Free-text finish criteria. */
  finish_criteria: string;
  /** Ordered list of phases (vertical layout). */
  phases: SkillPhase[];
}

// ── Run timeline ───────────────────────────────────────────────────────────

/**
 * Visual category for a `RunEvent`.
 */
export type EventType = "primary" | "info" | "success" | "warn" | "error";

export interface RunEvent {
  /** Sequential id within the run. */
  id: number;
  /** Visual category. Absent when the entry is a phase marker. */
  type?: EventType;
  /** Engine event name (e.g. "llm_called"). */
  name?: string;
  /** ISO-like timestamp (e.g. "14:23:11.088"). */
  ts?: string;
  /** Humanized description. */
  desc?: string;
  /** Phase id, present when the entry is a phase marker (no type/name/ts). */
  phase?: string;
  /** Whether this event is the default selection. */
  selected?: boolean;
}

export type RunStatus = "running" | "success" | "error";

export interface RunSummary {
  /** Run id (e.g. "r_4f3a92"). */
  id: string;
  /** Skill name. */
  skill: string;
  /** Started-at timestamp string. */
  started: string;
  /** Humanized duration (e.g. "2m 14s"). */
  dur: string;
  /** Current status. */
  status: RunStatus;
  /** Humanized cost string (e.g. "$0.087"). */
  cost: string;
  /** Default selection in the table. */
  selected?: boolean;
}

// ── Permissions ────────────────────────────────────────────────────────────

export type PermissionRuleKind = "allow" | "prompt" | "deny";

export interface PermissionRule {
  /** Op identifier (e.g. "file.read", "shell"). */
  op: string;
  /** Pattern this rule applies to. */
  glob: string;
  /** Rule outcome. */
  rule: PermissionRuleKind;
  /** Optional human-readable note. */
  note?: string;
}

// ── i18n ───────────────────────────────────────────────────────────────────

/**
 * Every key consumed by the design's `t()` helper. The `en` and `ja`
 * objects in `ReynUiData.COPY` MUST mirror each other key-for-key.
 */
export interface I18nKeys {
  today_morning: string;
  today_sub: string;
  recap_label: string;
  your_agents: string;
  quickstarts: string;
  seeAll: string;
  chat: string;
  profile: string;
  open_studio: string;
  back_app: string;
  studio: string;
  app: string;
  today: string;
  agents: string;
  library: string;
  skills: string;
  runs: string;
  evals: string;
  topologies: string;
  settings: string;
  send: string;
  placeholder: string;
  permissions: string;
  new_agent: string;
  add_agent: string;
  library_lead: string;
  agents_lead: string;
}

// ── Action / Channel types (typed view of OPENUI_HOST for this schema) ────

/**
 * Shape of the action map for `TypedOpenUIHost<ReynUiActions, ReynUiChannels>`.
 *
 * Each entry maps an action name to its payload and return-value shapes.
 */
export interface ReynUiActions {
  "agent.submit": { payload: { agentId: string; text: string }; returns: void };
  "agent.intervention.answer": {
    payload: { choiceId?: string; text?: string };
    returns: void;
  };
  "data.refetch": { payload?: undefined; returns: ReynUiData };
}

/**
 * Shape of the channel map for `TypedOpenUIHost<ReynUiActions, ReynUiChannels>`.
 *
 * Each entry maps a channel name to its event shape.
 */
export interface ReynUiChannels {
  "agent.message": ChatMessage;
  "run.started": { runId: string; skillName: string; agentId: string };
  "run.finished": { runId: string; status: "ok" | "failed" | "aborted" };
  "phase.started": { runId: string; phaseId: string };
  "phase.finished": { runId: string; phaseId: string; status: PhaseStatus };
  "state.delta": { patch: JsonPatch };
  "budget.updated": { tokensToday: number; usdToday: number; tokensMonth: number; usdMonth: number };
}

/**
 * RFC 6902 JSON Patch operation.
 *
 * @see https://datatracker.ietf.org/doc/html/rfc6902
 */
export type JsonPatchOp =
  | { op: "add"; path: string; value: unknown }
  | { op: "remove"; path: string }
  | { op: "replace"; path: string; value: unknown }
  | { op: "move"; path: string; from: string }
  | { op: "copy"; path: string; from: string }
  | { op: "test"; path: string; value: unknown };

export type JsonPatch = JsonPatchOp[];
