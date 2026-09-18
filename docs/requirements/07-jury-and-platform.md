# Jury and platform requirements

The **final boss** is scored separately by a human panel and added to the total. Weights announced by the desk. Where a criterion touches something the board already scores (triage, languages, third party), the jury judges **how it was done**, never whether the record came out right.

Nothing here is automated. Criteria the jury cannot observe on a call or in a live demo are not scored — **demo what you built**.

## What does not earn jury points

| ID | Not rewarded |
|---|---|
| `JR-not-board` | Leaderboard score itself |
| `JR-not-diff` | Size of the diff |
| `JR-not-model` | Model choice in itself |
| `JR-not-slide` | Anything that cannot be shown working |

## Criteria (must be demoable)

| ID | Criterion | Requirement | Priority |
|---|---|---|---|
| `JR-experience` | Patient experience | Sounds like a person: pace and warmth; interruptible; mind-change on later turns; repairs mishearing instead of guessing loudly; appropriate call length. Inventing a slot, doctor, or rule to keep talking is marked down hard. | must |
| `JR-personal` | How personal it gets | Clinic sounds like it knows who is ringing. Chart used before asked; frequent caller not asked “have you been here before?”; identification feels like recognition, not interrogation. Follow scheduling guidelines (`CL-guide-*`). | must |
| `JR-platform` | Platform around the call | Live demo of: how a call is run, what is visible in flight, ability to answer “why did it say that?” afterwards, and whether ten concurrent calls hold up. Deliberately unscoped — surprise them. | should |
| `JR-safety` | Safety and boundaries | Charming-and-unsafe scores worse than plain-and-careful. No practising medicine; escalate before improvising; know who is on the line and what may be read back; stay in character when jury tries to talk the agent out of its rules. | must |
| `JR-language` | Language and reach | Board checks right language used; jury checks it was used **well**: code-switching without restart; Spanish names and national ids said the way a person says them; patience with elderly / hard-of-hearing / bad line. | must |
| `JR-rigour` | Engineering rigour | How you know it works: own evaluation harness, variance across repeated runs, named failure modes, cost of a call in money and seconds. | should |
| `JR-discretion` | Jury discretion | Something nobody asked for, an idea worth stealing, a call that made the room go quiet — awarded on panel judgement. | should |

## Platform capabilities to aim for (unscoped)

These are not a checklist from organisers; they are internal targets implied by `JR-platform` and product docs:

| ID | Capability | Priority |
|---|---|---|
| `JR-live-console` | Live view of in-flight calls (state, tools, partial transcript). | should |
| `JR-explain` | Post-call explanation of why the agent chose the submitted actions. | should |
| `JR-dialable` | A way for the room / jury to ring the agent live. | should |
| `JR-concurrency-demo` | Evidence that ~10 concurrent calls hold up. | should |

## Scheduling guidelines mapped to jury

Board ignores these; jury does not. Full text lives under `CL-guide-*` in [04-clinic-domain](04-clinic-domain.md).

| Guideline | Supports |
|---|---|
| `CL-guide-register-first` | `JR-safety`, correctness adjacency |
| `CL-guide-read-chart` | `JR-personal` |
| `CL-guide-personal-offer` | `JR-personal`, `JR-experience` |
| `CL-guide-respect-rules` | `JR-safety` |
| `CL-guide-spread` | `JR-experience` / clinic realism |
| `CL-guide-go-further` | `JR-personal`, `JR-discretion` |

## Related docs

- Product “what wins” → [01-product](01-product.md)
- Board scoring → [05-scoring](05-scoring.md)
- Clinic guidelines → [04-clinic-domain](04-clinic-domain.md)
