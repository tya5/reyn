"""reyn.core.part_types — the discoverable part-type marker package (proposal
``docs/deep-dives/proposals/0060-llm-wielding-foundation.md`` §2.4/§3 F1).

Each submodule here declares ONE part-type by exporting a module-level
``PART_TYPE_SPEC: PartTypeSpec`` marker (see
``reyn.core.part_type_registry``). The meta-registry is BUILT by walking this
package and collecting every marker — there is no central list. A new
part-type is added by dropping a new marked submodule into this directory and
touching nothing else; a submodule without the marker is simply not a
part-type. This is the mechanism that makes forgotten-registration drift
impossible by construction.

Each marker's ``registry_ref`` points at the REAL per-part-type registry the
part-type's live instances come from (skills/pipelines/presentations
builders, the hook loader, the MCP roster enumerator) — those registries live
in their own subsystems; this package only carries the thin marker that ties
them into the unified part-type taxonomy.
"""
