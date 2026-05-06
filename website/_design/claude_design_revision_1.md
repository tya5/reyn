# Claude Design Revision 1 — Nav + Fixes

Paste this into Claude Design as a follow-up message on the same design.

---

```
Please apply the following changes to the current design:

1. Add a minimal navigation to the header (right side of the hero-top bar).
   Include two links:
   - "Docs" → href="/docs"
   - "GitHub" → href="#" (placeholder)
   Style: small text (14px), weight 400, color var(--fg-2), hover to var(--fg).
   No buttons, no background, no borders. Just text links with a gap of 32px between them.

2. Fix the footer license text.
   Change: "open source under Apache 2.0"
   To:     "open source under MIT"

3. Remove all data-screen-label attributes from every section element.

4. Fix the "Read the docs" anchor href.
   Change: href="#"
   To:     href="/docs"

No other changes. Keep everything else exactly as-is.
Output the full updated index.html.
```
