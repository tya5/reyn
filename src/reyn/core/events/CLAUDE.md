# Rules for `src/reyn/core/events/`

- **Audit-event kinds are a closed vocabulary.** Emitting a kind, declaring it in `AUDIT_EVENT_KINDS` (`event_schema.py`), and enumerating it in `docs/reference/runtime/events.md` is ONE three-part change. **CI checks the enumeration only — the semantic table row is on you.**

Emitters live all over `src/`, so a session that only emits may never load this
file. CI still fails on two parts without the third; the uncaught half is the
semantic table row.
