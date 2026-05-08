/**
 * OpenUI Layer 1 — TypeScript types for a schema manifest.
 *
 * A manifest describes a Layer 1 schema: its data shape, actions,
 * channels, and component contracts. Schemas declare these in YAML or
 * JSON files; this module provides the TypeScript shape of those
 * documents for tooling (validators, type checkers, IDE completion).
 *
 * See ../spec/manifest.md for the prose specification.
 *
 * @packageDocumentation
 */

/**
 * Top-level shape of a schema manifest file.
 */
export interface OpenUIManifest {
  /** `<domain>/<version>` identifier (e.g. `"reyn-ui/v1"`). */
  schema: string;
  /** One-line human-readable title. */
  title: string;
  /** Multi-paragraph description. */
  description: string;
  /** Layer 0 spec version targeted (e.g. `"1.0"`). */
  spec_version: string;
  /** Top-level data shape descriptor. */
  data: ManifestData;
  /** Map of action name → action descriptor. May be empty. */
  actions: Record<string, ManifestAction>;
  /** Map of channel name → channel descriptor. May be empty. */
  channels: Record<string, ManifestChannel>;
  /** Map of component name → component descriptor. */
  components: Record<string, ManifestComponent>;
  /** Free-form notes; never load-bearing. */
  extensions?: Record<string, unknown>;
}

/**
 * Descriptor for the `OPENUI_DATA` shape exposed by this schema.
 */
export interface ManifestData {
  /** Type name referenced from the sibling `data.types.ts`. */
  type: string;
  /** Optional human-readable description. */
  description?: string;
}

/**
 * Descriptor for one action in `manifest.actions`.
 */
export interface ManifestAction {
  /** What this action does, when designs invoke it. */
  description: string;
  /**
   * Payload shape. Each entry maps a field name to a TypeScript-style
   * type expression. `?` suffix on the field name marks the field
   * optional. Omit `payload` entirely if the action takes none.
   */
  payload?: Record<string, string>;
  /**
   * Return-value type, as a TypeScript-style type expression. Use
   * `"void"` when no value is returned.
   */
  returns: string;
}

/**
 * Descriptor for one channel in `manifest.channels`.
 */
export interface ManifestChannel {
  /** When this channel emits and what it carries. */
  description: string;
  /**
   * Event shape, as a TypeScript-style type expression or inline shape
   * (e.g. `"ChatMessage"`, `"{ patch: JsonPatch }"`).
   */
  event: string;
}

/**
 * Surface a component belongs to.
 *
 * - `app`: end-user-facing surface.
 * - `studio`: developer-facing surface.
 * - `shared`: appears on both surfaces with the same component.
 */
export type ComponentSurface = "app" | "studio" | "shared";

/**
 * Descriptor for one component in `manifest.components`.
 */
export interface ManifestComponent {
  /** Which surface (App face, Studio face, both). */
  surface: ComponentSurface;
  /**
   * Whether designs MUST export this component. `false` lets a design
   * skip a component the host is willing to fall back on or hide.
   */
  required: boolean;
  /**
   * Prop shape. Each entry maps a prop name to a TypeScript-style type
   * expression. `?` suffix on the prop name marks the prop optional.
   */
  props: Record<string, string>;
}
