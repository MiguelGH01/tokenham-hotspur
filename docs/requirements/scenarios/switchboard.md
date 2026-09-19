# Scenarios — `PR-02` Switchboard (`switchboard`)

Weight **none** (diagnostic). Run All does **not** dial this problem. Trigger manually before first scored run.

Every burst opens N independent problem-1 bookings at the same moment — each with its own patient, socket, and `call_id`. No single accepted JSON is shown on the dashboard (`accepted: []`).

## Scenario-derived requirements

| ID | Requirement | Priority |
|---|---|---|
| `PR-02-S1` | Handle concurrent dials of **5**, **10**, and **20** without shared conversation / `call_id` state. | must |
| `PR-02-S2` | Each socket in a burst is a full `PR-01`-class booking; isolate pipelines per connection (`FR-concurrency`, `CR-concurrency`). | must |
| `PR-02-S3` | Do not expect Run All or leaderboard points from this problem. | must |

---

## Case `switchboard-burst-5`

| | |
|---|---|
| **Ask** | 5 problem-1 calls opened on the endpoint at the same moment. |
| **dials** | 5 |
| **Accepted** | Per-call problem-1 answers (not published as one payload). |
| **Hits** | `PR-02-S1`, `PR-02-S2`, `FR-concurrency` |

## Case `switchboard-burst-10`

| | |
|---|---|
| **Ask** | 10 problem-1 calls at once. |
| **dials** | 10 |
| **Accepted** | Per-call problem-1 answers. |
| **Hits** | `PR-02-S1`, `PR-02-S2`, `CR-concurrency` |

## Case `switchboard-burst-20`

| | |
|---|---|
| **Ask** | 20 problem-1 calls at once. |
| **dials** | 20 |
| **Accepted** | Per-call problem-1 answers. |
| **Hits** | `PR-02-S1`, `PR-02-S2` |

## Implementation notes

- No single `POST` covers the burst; each dial submits its own `/submit/book` (or whatever that line requires).
- Keep the tunnel and process up for the whole burst — a dropped connection fails only that dial’s case.
