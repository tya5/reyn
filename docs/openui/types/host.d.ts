/**
 * OpenUI Layer 0 — TypeScript types for the host adapter.
 *
 * Designs that import these types get IDE help when calling the host;
 * hosts implementing the protocol can typecheck their adapter against
 * `OpenUIHost`.
 *
 * See ../spec/layer-0.md for the prose specification.
 *
 * @packageDocumentation
 */

/**
 * The host adapter object. A conforming host sets `window.OPENUI_HOST` to
 * an instance of this interface before loading the design's entry script.
 */
export interface OpenUIHost {
  /**
   * Invoke a host-side action. The action name's vocabulary is defined by
   * the active Layer 1 schema; payload and return-value shapes follow the
   * schema's `actions` declaration.
   *
   * The reserved action `data.refetch` is always available (Layer 0 § 4.1)
   * and returns a fresh snapshot of `OPENUI_DATA`-shaped data.
   *
   * Rejects with an `Error` instance on failure (unknown action, payload
   * validation error, backend failure). The `message` is human-readable;
   * schemas may attach structured details via `Error.cause` or custom
   * properties.
   */
  invoke(action: string, payload?: unknown): Promise<unknown>;

  /**
   * Subscribe to a host-emitted channel. The channel name's vocabulary is
   * defined by the active Layer 1 schema; event shapes follow the
   * schema's `channels` declaration.
   *
   * Returns an unsubscribe function. Calling it more than once is a no-op.
   *
   * Multiple subscribers per channel are permitted. A throwing handler
   * MUST NOT prevent other handlers on the same channel from receiving
   * events.
   */
  listen(
    channel: string,
    handler: (event: unknown) => void,
  ): () => void;
}

/**
 * The shape of `window.OPENUI_*` globals that a conforming host populates
 * before loading the design.
 *
 * Designs read these as `window.OPENUI_HOST` etc. and MUST NOT reassign.
 */
export interface OpenUIWindow {
  /**
   * The host adapter. See `OpenUIHost`.
   */
  OPENUI_HOST: OpenUIHost;

  /**
   * Initial data, populated by the host before mount. Shape is determined
   * by the active Layer 1 schema; designs MAY refine this `unknown` to
   * the schema's specific data type.
   */
  OPENUI_DATA: unknown;

  /**
   * The active Layer 1 schema identifier in the form
   * `<domain>/<version>` (e.g. `"reyn-ui/v1"`).
   */
  OPENUI_SCHEMA: string;

  /**
   * Whether the design is being previewed standalone (true) or embedded
   * in a host (false). Hosts SHOULD set this to `false` before loading.
   * Standalone designer previews default to `true`.
   */
  OPENUI_DESIGN_MODE: boolean;
}

/**
 * Augment the global `Window` interface so designs can read the
 * OpenUI globals without manual casting.
 */
declare global {
  interface Window extends OpenUIWindow {}
}

/**
 * Helper: a typed view of `OPENUI_DATA` for a known schema. Designs
 * targeting `reyn-ui/v1` would write:
 *
 *     import type { ReynUiData } from "../schemas/reyn-ui-v1/data.types";
 *     const data = window.OPENUI_DATA as ReynUiData;
 *
 * or use this helper:
 *
 *     const data = openuiData<ReynUiData>();
 */
export function openuiData<T>(): T {
  return window.OPENUI_DATA as T;
}

/**
 * Helper: a typed view of `OPENUI_HOST` with schema-specific action and
 * channel signatures. Schema authors can declare an `actions` and
 * `channels` map and use this to get strong typing:
 *
 *     interface ReynUiActions {
 *       "agent.submit": { payload: { agentId: string; text: string }; returns: void };
 *       "data.refetch": { payload?: undefined; returns: ReynUiData };
 *     }
 *     interface ReynUiChannels {
 *       "agent.message": ChatMessage;
 *       "state.delta": { patch: JsonPatch };
 *     }
 *
 *     const host = openuiHost<ReynUiActions, ReynUiChannels>();
 *     await host.invoke("agent.submit", { agentId, text });
 *     host.listen("agent.message", (msg) => { /* msg is ChatMessage *\/ });
 */
export interface TypedOpenUIHost<
  Actions extends Record<string, { payload?: unknown; returns: unknown }>,
  Channels extends Record<string, unknown>,
> {
  invoke<K extends keyof Actions & string>(
    action: K,
    payload?: Actions[K]["payload"],
  ): Promise<Actions[K]["returns"]>;

  listen<K extends keyof Channels & string>(
    channel: K,
    handler: (event: Channels[K]) => void,
  ): () => void;
}

export function openuiHost<
  Actions extends Record<string, { payload?: unknown; returns: unknown }> = Record<
    string,
    { payload?: unknown; returns: unknown }
  >,
  Channels extends Record<string, unknown> = Record<string, unknown>,
>(): TypedOpenUIHost<Actions, Channels> {
  return window.OPENUI_HOST as unknown as TypedOpenUIHost<Actions, Channels>;
}
