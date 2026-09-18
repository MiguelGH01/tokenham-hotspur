# Agent structure (El Turno)

Process map and Pipecat Flows graph for the Clínica Arenal receptionist. Requirements stay in [`docs/requirements/`](../requirements/01-product.md); this folder is **how the agent walks them**.

| File | Role |
|---|---|
| [process-map.md](process-map.md) | Workflows, rails, fallbacks, state, problem mapping |
| [flow.yaml](flow.yaml) | `FlowConfig` graph (nodes, tools, `transition_to` branches) |

**Contract:** the LLM talks and extracts; **code** owns directory match, dates, nearest site, appointment type, leave/site fallbacks, `blocked` → `reason`, and every `POST /submit/*`. The model never authors `patient_id`, `slot`, or `reason`.

This YAML is the intended graph. It is **not** loaded by `server/bot.py` yet. Handlers named here are the Python module to implement next.
