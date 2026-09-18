# How to re-fetch the Prosper docs

The official docs live at `https://hackspain.getprosperapp.com/leaderboard/docs`
and render as a **client-side JS SPA**. Plain `curl`/`WebFetch` returns only
the literal word "Prosper" — no content — because nothing renders without JS.

To read them, use a headless-browser tool (e.g. chrome-devtools MCP,
Playwright): navigate to the URL, then take an accessibility-tree / DOM
snapshot of the rendered page. A plain HTTP fetch will look empty; that does
not mean the page is broken.

## Page map (all under `/leaderboard/docs/`)

| Path | Title | Content |
|---|---|---|
| `/overview` | The short version | One-page summary: what to build, weekend schedule, scoring |
| `/challenge` | What Is the Challenge? | Full challenge description + "Who wins" (jury criteria breakdown) |
| `/quickstart` | Get on the phone | Account/key setup, WebSocket server build, ngrok tunneling, dashboard endpoint config, call-yourself loop |
| `/contract` | The call contract | Twilio Media Streams wire format, submission window, `/api/v1/submit/<action>` payloads, `reason` enum, gotchas |
| `/clinic-api` | The clinic | Read-only EHR endpoints, domain traps, scheduling guidelines (jury-judged, not leaderboard-scored) |
| `/rules` | Scoring | What passes a case, points formula, call limits, failure-attribution table, recordings/reveal policy |
| `/problems` | The 18 problems | Full problem list with weights, open status, per-problem answer shape |
| `/api` | API Documentation | Generated OpenAPI/Swagger reference for the clinic + submission routes |

`docs/prosper-track-reference.md` in this repo is a snapshot of all of the
above taken 2026-09-18. Re-fetch if the organizers announce a rules change
(the Scoring page carries a version string, e.g. "Version 2.0-draft · 17
September 2026" — check that against the snapshot date before trusting old
notes) or if a problem's "open" status needs checking during the event.

## When something is unclear, consult before guessing

If a rule, an API field's exact behavior, a `reason` code, a problem's
answer shape, or anything scoring-affecting is ambiguous while working on
this codebase, don't guess and don't assume the snapshot is complete —
Prosper's own docs explicitly say a "no way to say it makes its case
unanswerable" and reward reading the rule off the API, not inferring it.

1. **Check `docs/prosper-track-reference.md` first** — it's local, no
   network needed, and covers everything gathered so far.
2. **If it doesn't resolve the question, or the detail is one that changes
   during the event** (a problem's `open` status, its weight, the Scoring
   page's version string) **fetch the live page directly** using the page
   map above and a browser-capable tool — plain HTTP fetch will not work,
   see the SPA note above. Whichever agent/tool is running this task should
   use its own browser-rendering capability (a devtools/browser MCP tool,
   or Playwright, or ask the human operator to check the page) rather than
   answer from a possibly-stale snapshot.
3. **If the live page contradicts the snapshot**, treat the live page as
   correct, and update `docs/prosper-track-reference.md` (and this file's
   page map, if a page was added/removed/renamed) so the next lookup is
   fast again.

This applies regardless of which coding agent/harness is running — Claude
Code has a dedicated skill for full re-scrapes
(`.claude/skills/refresh-prosper-docs/`) and a lighter one for one-off
clarification lookups (`.claude/skills/consult-prosper-docs/`); Cursor has
the equivalent guidance in `.cursor/rules/prosper-docs-lookup.mdc`. Both
just point back to this procedure.
