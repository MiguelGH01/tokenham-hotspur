---
name: refresh-prosper-docs
description: Re-scrape the Prosper/HackSpain track docs (hackspain.getprosperapp.com/leaderboard/docs) and update docs/prosper-track-reference.md. Use when the user asks to refresh, re-scrape, re-check, or sync the Prosper docs, when a problem's "open" status needs checking during the event, or when the organizers announce a rules change.
---

# Refresh Prosper docs

The Prosper track docs are a **client-side-rendered JS SPA**. Plain
`WebFetch`/`curl` returns nothing but the literal word "Prosper" — you must
render the page in a browser. Use the `chrome-devtools` MCP tools
(`new_page`, `navigate_page`, `take_snapshot`) for this, not WebFetch.

If those tools aren't already loaded this session, load them first:

```
ToolSearch("select:mcp__plugin_ecc_chrome-devtools__navigate_page,mcp__plugin_ecc_chrome-devtools__take_snapshot,mcp__plugin_ecc_chrome-devtools__new_page")
```

## Steps

1. **Open the docs root** to catch any change to the page list itself (a new
   page in the sidebar, a renamed one):
   `https://hackspain.getprosperapp.com/leaderboard/docs`
   Take a snapshot; read the `navigation` links in the sidebar.

2. **Visit every page** in the sidebar (currently these eight — update this
   list if step 1 shows a different set) with `navigate_page` on the same
   `pageId`, then `take_snapshot` after each navigation:
   - `/overview` — The short version
   - `/challenge` — What Is the Challenge?
   - `/quickstart` — Get on the phone
   - `/contract` — The call contract
   - `/clinic-api` — The clinic
   - `/rules` — Scoring
   - `/problems` — The 18 problems
   - `/api` — API Documentation (the generated OpenAPI/Swagger reference —
     skim only unless the user asks for exact request/response schemas)

3. **Check the version marker.** The Scoring page (`/rules`) prints a version
   string near the top, e.g. "Version 2.0-draft · 17 September 2026". Compare
   it against the "Scraped from ... on <date>" line at the top of
   `docs/prosper-track-reference.md`. If unchanged and nothing else in the
   snapshots differs from the existing file, say so and stop — no need to
   rewrite.

4. **On the problems page specifically**, pay attention to the `Open` column
   (yes / not yet) and each problem's weight — these change during the event
   as problems are released, and are the fields most likely to go stale
   between scrapes.

5. **Update `docs/prosper-track-reference.md`**: rewrite it to match the
   current site content, preserving its existing section structure (What you
   build → Event logistics → Getting on the phone → The call contract → The
   clinic → Scoring → The 18 problems → Jury scoring). Bump the "Scraped
   from... on <date>" line at the top to today's date. Do not touch
   `docs/prd.md`, `docs/build-plan.md`, or `docs/18-cases-mapping.md` — those
   are the team's own planning artifacts, not a docs mirror.

6. **Update `docs/prosper-docs-navigation.md`'s page table** only if step 1
   revealed an added/removed/renamed page.

7. **Report a short diff summary** to the user: what changed (e.g. "problem 5
   is now open", "problem 9's weight changed from 3 to 4", "no changes since
   last scrape"). Don't just say "updated the docs" — the whole point of this
   skill is catching what moved.
