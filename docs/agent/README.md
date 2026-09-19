# Agent structure (El Turno)

Process map and Pipecat Flows graph for the Clínica Arenal receptionist. Requirements stay in [`docs/requirements/`](../requirements/01-product.md); this folder is **how the agent walks them**.

| File | Role |
|---|---|
| [process-map.md](process-map.md) | Workflows, rails, fallbacks, state, problem mapping |
| [flow.yaml](flow.yaml) | `FlowConfig` graph (nodes, tools, `transition_to` branches) |
| [add-pr04-06.md](add-pr04-06.md) | Handoff: wire register / relative dates / clinic-rule branches into the bot |

**Contract:** the LLM talks and extracts; **code** owns directory match, dates, nearest site, appointment type, leave/site fallbacks, `blocked` → `reason`, and every `POST /submit/*`. The model never authors `patient_id`, `slot`, or `reason`.

This YAML is the intended full graph (invariants). `server/bot.py` runs three
Python nodes — identify, act, close — in `server/flow/`. Clinic branches are
tool results, not extra nodes.

When wiring [add-pr04-06.md](add-pr04-06.md): public cases are tests. The bot must still behave if the caller, date phrase, or plan×specialty pair is one we have not seen — drive decisions from the clinic bundle and `/availability`, not from copied answer JSON.
