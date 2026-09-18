# 18 test cases → capability mapping

Reconstructed from the Prosper Track brief (not verified against HackSpain's literal internal list — cross-check against the practice cases once available).

| # | Case | Bucket | Mechanism |
|---|---|---|---|
| 1 | Straightforward booking | Core flow | Happy path: identify → slot → confirm → book |
| 2 | Ten straightforward bookings at once | Scale | Stateless-per-call session handling, no shared mutable state |
| 3 | Caller unknown to records | Identity resolution | 0-match branch → create new patient record with min. required fields |
| 4 | Caller matches four people | Identity resolution | N-match branch → disambiguate via DOB/phone (1-2 questions) |
| 5 | Specific doctor requested | Slot resolution | Availability query filtered by doctor_id |
| 6 | Specific site requested | Slot resolution | Availability query filtered by site_id |
| 7 | "Soonest" requested | Slot resolution | Availability query sorted by date, no filter |
| 8 | Vague time ("next Thursday", "first thing Monday") | Time normalization | Relative-date parser anchored to `now` in Europe/Madrid + clinic hours |
| 9 | Rule-forbidden request | Policy engine | Rules table checked pre-booking, returns reason_code, must speak it |
| 10 | Full diary, nothing free | Slot resolution | No-match branch → waitlist/alt-site offer, not dead-end |
| 11 | Reschedule | Mutation flow | Find existing appt → confirm identity → confirm change → write |
| 12 | Cancel | Mutation flow | Same as above, delete branch |
| 13 | Third-party caller (parent/child, daughter/father) | Identity resolution | Caller identity ≠ patient identity, verify relationship |
| 14 | Should be sent to a doctor, not booked | Escalation | Always-on triage classifier runs parallel to intent routing |
| 15 | Non-English / regional Spanish languages | Multilingual | Detect language turn 1, pin ASR/TTS, logic stays in code not prompt |
| 16 | Terrible line (audio quality) | Robustness | Confirm-back critical fields, graceful re-ask on low ASR confidence |
| 17 | Interrupts/corrects/changes mind | Robustness | Re-entrant slots in state machine, barge-in support from voice provider |
| 18 | Social engineering ("talk it into something") | Policy engine / Robustness | Policy checks are code-enforced, not prompt-overridable; caller text treated as data |
