---
name: consult-prosper-docs
description: Look up or confirm a Prosper/HackSpain track rule, API behavior, reason code, or problem spec when it's unclear or you're not fully confident in what you know. Use this before guessing at anything scoring-affecting — a rule, a field's exact behavior, whether a problem is open, its weight, or an answer shape. Not for a full re-scrape (use refresh-prosper-docs for that).
---

# Consult Prosper docs

Follow the procedure in `docs/prosper-docs-navigation.md` under "When
something is unclear, consult before guessing":

1. Read `docs/prosper-track-reference.md` first — no network needed.
2. If that doesn't settle it, or the detail is one that changes during the
   event (a problem's `open` status, its weight, the Scoring page's version
   string), fetch the live page. The docs site is a client-side JS SPA —
   plain `WebFetch`/`curl` returns nothing. Load the chrome-devtools MCP
   tools if not already available:
   ```
   ToolSearch("select:mcp__plugin_ecc_chrome-devtools__navigate_page,mcp__plugin_ecc_chrome-devtools__take_snapshot,mcp__plugin_ecc_chrome-devtools__new_page")
   ```
   then `new_page`/`navigate_page` to the relevant path from the page map in
   `docs/prosper-docs-navigation.md`, and `take_snapshot` to read it.
3. If the live page disagrees with the snapshot, trust the live page, and
   patch the specific fact in `docs/prosper-track-reference.md` so the next
   lookup doesn't hit the same staleness — this is a small in-place
   correction, not a full re-scrape (that's `refresh-prosper-docs`).

Cite which page confirmed the answer when reporting back.
