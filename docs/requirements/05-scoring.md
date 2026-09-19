# Scoring requirements

Automatic score only. Jury final boss is separate — see [07-jury-and-platform](07-jury-and-platform.md).  
Official scoring version noted in source: **2.0-draft · 17 September 2026**.

## Pass / fail

| ID | Requirement | Priority |
|---|---|---|
| `SC-binary` | A case passes or fails. No partial credit within a case (not for a field, not for most of a name, not for an id one character out). | must |
| `SC-membership` | Pass if the submitted **list of actions** matches **any** member of the case’s acceptable set, after normalization. | must |
| `SC-multi-ok` | More than one answer can be correct (e.g. three GPs free at the same earliest minute). Membership, not partial credit. | must |
| `SC-no-silence` | Correct refusal still requires `NO_ACTION` (or escalate) with reason. Empty list / no submission always fails. | must |
| `SC-conversation-unscored` | Voice, manner, personalisation, load-spreading are **not** scored on the board. | must |
| `SC-expected-via-api` | Expected answers are computed through the same availability use case the agent calls — cases never expect a slot the API would not offer. | must |

## Two lanes

| ID | Requirement | Priority |
|---|---|---|
| `SC-practice` | Practice dials one published case (answer included). Scores nothing. Feedback: transcript, recording, fields lost (not what they should have been). | must |
| `SC-run-all` | Run All is the scored lane: **four private cases** per scored problem currently open, dialled **10 at a time**. Team chooses nothing about the set. | must |
| `SC-private` | Private cases generated per run; answers never published. While scoring open: pass/fail, attribution, failure signal only — no transcript/audio/field detail until reveal. | must |
| `SC-one-run` | One queued or active run at a time (either lane). | must |
| `SC-cooldown-practice` | 30 seconds between practice calls. | must |
| `SC-cooldown-run` | 15 minutes after last Run All **finished** before the next may start. Run All ~18 minutes when full roster open → roughly every ~33 minutes. | must |
| `SC-problem-2` | Problem 2 (Switchboard) carries no weight; Run All never dials it. Practice never scores. | must |

## Points

| ID | Requirement | Priority |
|---|---|---|
| `SC-formula` | Per problem: (fraction of its four cases passed: 0, .25, .5, .75, 1) × difficulty weight (1–5). Team score = **sum** over problems. No percentage, no denominator. | must |
| `SC-max` | Full roster max score: **196** (points = passed cases × weight; live docs 19 Sep 2026 — earlier capture said 49, the sum of weights alone). | must |
| `SC-best-run` | Leaderboard ranks each team’s **best** Run All (not latest, not cumulative). Board shows how many runs backed a score. | must |
| `SC-unattempted` | A problem nobody attempted scores nothing (same as dialled and failed). Silence is never cheaper than a wrong answer. | must |
| `SC-progressive` | Problems open progressively. Opening a new problem never changes scores of runs taken before it. | must |
| `SC-harness-exclude` | Attributed harness failures are excluded rather than failed (see attribution). | must |

## Call limits

| ID | Requirement | Priority |
|---|---|---|
| `SC-three-minutes` | Every call capped at **three minutes**. | must |
| `SC-silence-cut` | No audible audio from the agent for the silence window → cut off, attributed to agent. Streaming silence ≠ speaking. | must |
| `SC-cut-still-attempt` | A call cut off this way is still an attempt; without an accepted record it scores nothing. | must |

## What is not scored (board)

| ID | Not scored |
|---|---|
| `SC-not-voice` | Voice quality, accent, naturalness, politeness, style |
| `SC-not-asr` | Transcription accuracy alone, spelling aloud |
| `SC-not-process` | Number/order of questions, tool calls, confirmations |
| `SC-not-cost` | Model choice, architecture, tokens, provider cost |
| `SC-not-speed` | Speed (limits can still kill a late record) |

Exception: `PR-14` also checks the transcript for leaked protected fields.

## Deadlines and reveal

| ID | Requirement | Priority |
|---|---|---|
| `SC-freeze` | Wall freezes **Sunday 20 September 2026, 06:00 Europe/Madrid**. Only runs completed at or before that instant count. Equal scores share rank (1, 1, 3). | must |
| `SC-reveal` | Private-case transcript/audio detail opens **Monday 21 September 2026, 00:00 Europe/Madrid**. Expected values of private cases are **never** published. | must |
| `SC-corrections` | Rule changes announced to every team with old/new wording, reason, effective time. Wire/schema stay backward compatible for the weekend. | must |
| `SC-disputes` | Disputes: give organisers team, run and call ids, rules version, expected rule, observation. | must |

## When a call fails (attribution)

Attribution is deterministic (no LLM arbiter).

| Evidence | Attribution | Run treatment |
|---|---|---|
| Matching record, no failure signals | none | Case passes |
| Missing/mismatching record, no infrastructure signal | `agent_issue` | Case fails |
| Endpoint unreachable, malformed agent message, or clean early hang-up | `agent_issue` | Case fails |
| No audible audio for silence window | `agent_issue` | Case fails |
| Wall-clock limit, turn cap, unexplained disconnect, unidentified pipeline error | `inconclusive` | Case fails; evidence for investigation |
| Identified harness STT/LLM/TTS error, or confirmed local socket defect | `harness_issue` | **Entire run voided** |
| Confirmed harness defect **and** independently observed agent failure | `mixed` | **Entire run voided** |

| ID | Requirement | Priority |
|---|---|---|
| `SC-void` | Voided run contributes no score; releases cooldown for its mode; never silent-reruns. Request replacement explicitly. | must |
| `SC-feedback-scored` | Team sees identifiers, attribution, fixed signal codes only while scoring open — not raw provider errors, field names, private contents, or transcripts. | must |

## Normalization

Applies only where a human voice was in the loop: `REGISTER` demographics and appointment **slot**. Ids (`PR05`, etc.) are exact.

Accent-insensitive fold: Unicode NFKD + strip combining marks (also folds `ñ` → `n`).

### National id (DNI/NIE) — `SC-norm-nid`

| Submitted | Normalizes to |
|---|---|
| `12345678-Z` | `12345678Z` |
| `12345678z` | `12345678Z` |
| `1234 5678 Z` | `12345678Z` |
| `x-1234567-l` | `X1234567L` |

Check letter is re-derived from digits on register (separates misheard vs invented).

### Captured name — `SC-norm-name`

| Submitted | Normalizes to |
|---|---|
| `José García López` | `jose {garcia, lopez}` |
| `José López García` | `jose {garcia, lopez}` (surname order irrelevant) |
| `Ana Muñoz Ruiz` | `ana {munoz, ruiz}` |

### Phone — `SC-norm-phone`

| Submitted | Normalizes to |
|---|---|
| `+34 612 345 678` | `612345678` |
| `0034612345678` | `612345678` |
| `612-345-678` | `612345678` |

### Email — `SC-norm-email`

| Submitted | Normalizes to |
|---|---|
| `Ana.Garcia@Gmail.com` | `ana.garcia@gmail.com` |
| ` ana.garcia @ gmail.com ` | `ana.garcia@gmail.com` |

### Slot — `SC-norm-slot`

| Submitted | Normalizes to |
|---|---|
| `2026-09-19T10:30:07+02:00` | `2026-09-19T10:30:00+02:00` (seconds truncated) |
| `2026-09-19T08:30:00+00:00` | `2026-09-19T10:30:00+02:00` (→ Europe/Madrid) |

### Free-text enums — `SC-norm-enum`

| Submitted | Normalizes to |
|---|---|
| `  Review  ` | `review` |
| `NO_AVAILABILITY` | `no_availability` |
| `Paediátric_Review` | `paediatric_review` |

## Recordings consent

| ID | Requirement | Priority |
|---|---|---|
| `SC-recordings` | Connecting an agent agrees that calls are recorded (audio + timestamped transcripts) for debugging, judging, disputes, and Sunday stage; retained after the weekend. Other teams cannot read ours. | must |

## Related docs

- Submit contract → [03-call-and-submission](03-call-and-submission.md)
- Problems and weights → [06-problems](06-problems.md)
- Jury half → [07-jury-and-platform](07-jury-and-platform.md)
