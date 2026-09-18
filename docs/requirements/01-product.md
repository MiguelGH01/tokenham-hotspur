# Product requirements

What we are building, how the weekend is structured, and what actually wins. Derived from the official overview and challenge pages (captured 18 Sep 2026).

## Product definition

| ID | Requirement | Priority |
|---|---|---|
| `PD-01` | Build a **voice AI agent** that answers **inbound scheduling calls** for a clinic (Clínica Arenal), the way a receptionist would. | must |
| `PD-02` | On each call the agent must: pick up, establish who is calling and what they need, look them up in clinic records, find **real** availability, and book / move / cancel — or correctly refuse / escalate when booking is wrong. | must |
| `PD-03` | The voice model is **one component** of a system: real lookups, real availability, checks before any write is reported, state that survives mind-changes, and enough visibility to explain why the agent said what it said. | must |
| `PD-04` | Some calls must **not** end in a booking (clinic cannot do it, medical emergency, rules forbid it). Recognising those is part of the product, not an edge case. | must |
| `PD-05` | The clinic EHR is **read-only**. The agent reports the write it **would** have made via submission APIs; it does not mutate the clinic. | must |

## Weekend shape

| ID | Requirement | Priority |
|---|---|---|
| `PD-10` | Event window: **Friday 18 → Sunday 20 September 2026**. | must |
| `PD-11` | Team is opened by organisers at the desk (email, dashboard password, API key `pk-…`); no self-registration. | must |
| `PD-12` | Team stands up a reachable WebSocket endpoint the harness can dial. | must |
| `PD-13` | Practice against published cases (answers included) as often as rate limits allow; scores nothing. | must |
| `PD-14` | Scored lane is **Run All**: private cases across open scored problems; points go on the leaderboard. | must |
| `PD-15` | **Checkpoints:** twice over the weekend the board freezes and a prize goes to whoever is leading — being early pays. Exact windows announced by the desk. | should |
| `PD-16` | **Sunday final boss:** jury calls the agent themselves and scores separately; team must demo what was built around the call. | must |
| `PD-17` | Organisers may place smoke calls at every endpoint during the weekend (practice; free if they fail). | should |

## What actually wins

Order of emphasis from the official short version:

| ID | Requirement | Priority |
|---|---|---|
| `PD-20` | **Correctness first.** The leaderboard is the main prize and only rewards getting the submitted record exactly right. | must |
| `PD-21` | **Then the call.** A correct agent that is unpleasant to talk to loses the half of the marks the jury holds. | must |
| `PD-22` | **Then the platform.** Live console, dialable number for the room, way to see why the agent did what it did — deliberately unscored by an answer key; surprise them. | should |

## Out of scope for this product doc

- Wire formats, submit routes → [03-call-and-submission](03-call-and-submission.md)
- Clinic rules and catalogue → [04-clinic-domain](04-clinic-domain.md)
- Points formula and pass criteria → [05-scoring](05-scoring.md)
- Per-problem intents → [06-problems](06-problems.md)
- Jury criteria detail → [07-jury-and-platform](07-jury-and-platform.md)
- Keys, tunnels, dashboard settings → [08-operations](08-operations.md)
