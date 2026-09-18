# Agent structure (El Turno)

Process map and Pipecat Flows graph for the Clínica Arenal receptionist. Requirements stay in [`docs/requirements/`](../requirements/01-product.md); this folder is **how the agent walks them**.

| File | Role |
|---|---|
| [process-map.md](process-map.md) | Workflows, rails, fallbacks, state, problem mapping |
| [flow.yaml](flow.yaml) | `FlowConfig` graph (nodes, tools, `transition_to` branches) |
| [add-pr04-06.md](add-pr04-06.md) | Handoff: add **new** problems (register / dates / clinic rules) as engines, not case switches |

**Contract:** the LLM talks and extracts; **code** owns directory match, dates, nearest site, appointment type, leave/site fallbacks, `blocked` → `reason`, and every `POST /submit/*`. The model never authors `patient_id`, `slot`, or `reason`.

`docs/agent/flow.yaml` is the intended full graph. New work for this weekend’s extra problems is [add-pr04-06.md](add-pr04-06.md) only. Public JSON in [scenarios/](../requirements/scenarios/README.md) is tests, not answers to paste.
