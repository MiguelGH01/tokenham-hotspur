# API enums

From OpenAPI components (v0.1.0). Closed vocabularies — do not invent values.

## `Insurer`

Used as `policy_id` / `insurer` on submit bodies and as `availability?insurer=` values.

```
sanitas
adeslas
dkv
asisa
mapfre
caser
cigna
axa
nueva_mutua
privado
```

| ID | Note |
|---|---|
| `API-enum-privado` | `privado` is self-pay as a held plan, not an automatic fallback (`CL-privado`). |

## `AppointmentWindow`

Query `when` on patient appointments:

```
upcoming   # default — what they can still act on
past
all
```

## `OutcomeReason`

Used on `NO_ACTION` and `ESCALATE`. First eleven mirror clinic `RestrictionKind` one-for-one.

### Clinic restriction mirrors

```
not_eligible_age
referral_required
provider_not_in_network
specialty_not_covered
location_not_covered
insurer_referral_required
allowance_exhausted
provider_on_leave
location_hours
type_not_offered
patient_history
```

### Non-rule endings

```
no_availability
clinic_closed
patient_not_found
provider_not_found
caller_not_authorised
out_of_scope
medical_emergency
```

| ID | Typical use (from requirements / scenarios) |
|---|---|
| `provider_not_found` | Named doctor does not exist and caller refuses anyone else (`PR-03`) |
| `no_availability` | Window empty after negotiation (`PR-07`) |
| `out_of_scope` | Adversarial / privacy decline (`PR-14`) |
| `medical_emergency` | Triage red flags (`PR-10`) → prefer `ESCALATE` |
| `provider_on_leave` | When refusing rather than redirecting a leave case |
| `caller_not_authorised` | Third-party / auth failures when applicable |

Normalization of free-text enum submissions (scoring) folds case/accents/whitespace — see `SC-norm-enum`. Prefer sending canonical lowercase snake_case as above.
