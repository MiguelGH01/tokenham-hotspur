# fix-gpt41-flow-prompts

Feature: adjust two flow node prompts so gpt-4.1 (LLM_PROVIDER=openai) follows the
expected booking flow, as exposed by `make eval` (2/7 scenarios failing).

## Tasks

- [ ] T1: identify node — call search_patient as soon as name + one exact identifier are held
  - File: server/flows/identification.py (`create_identify_node` task_messages)
  - Evidence: simple_booking_ignacio turn 1 — gpt-4.1 asked for national ID + DOB
    instead of calling search_patient(id_type: phone)
  - Commit: <pending>
- [ ] T2: slot node — pass `specialty` when the caller states/confirm a specialty; `provider_name` only when no specialty is known
  - File: server/flows/booking.py (`create_slot_node` task_messages)
  - Evidence: doctor_site_clarification turn 3 — gpt-4.1 passed
    provider_name "Martín Sáez" instead of specialty "general_practice"
  - Commit: <pending>
- [ ] T3: verify — re-run both failing scenarios via `make eval-one` and confirm 7/7 suite health on the rest
  - Result: <pending>
- [ ] T4: clarify ignacio's judge criterion — gpt-4.1 as judge (build_eval_judge_llm reuses
  the bot LLM) reads "ask another question" literally and rejects the offer-confirmation
  question that the confirm node itself instructs ("Ask if that works"). Clarify the
  criterion in evals/simple_booking_ignacio.yaml; bot behavior is per design.
  - Commit: <pending>

## Notes

- Docs source: pipecat context-hub — per-node instructions belong in node
  `task_messages` with role `developer` (flows/nodes-and-messages.md,
  api-reference/pipecat-flows/types.md).
- get_earliest_slot schema already accepts both `specialty` and `provider_name`;
  the change is prompt guidance only, no schema/behavior change.
- Pre-existing dirty files in the worktree (Makefile, docs, server/booking.py,
  .gitignore) are NOT part of this feature; do not stage them.
