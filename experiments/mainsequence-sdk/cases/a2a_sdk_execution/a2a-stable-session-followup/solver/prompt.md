# Goal
Design the `eval-a1b004c9` implementation slice for this scenario: The team is working on an already deployed research Agent serving the staging environment. The requested slice is 'Preserve session identity' and the concrete outcome is to keep the same session identity across related A2A follow-up messages.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-a1b004c9-repo` and `eval-a1b004c9-env`.
- A first request already returned session `eval-a1b004c9-session`; a follow-up and one retry are now required.
- The governing boundary is: discover the existing Agent, preserve one AgentSession uid across related turns, honor its advertised message or task capabilities, and keep retries idempotent.
- A success claim must be supported by the selected Agent identity, stable session, response envelope or terminal task state, and bounded failure evidence.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
