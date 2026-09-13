The review should reject the proposal because it violates the **Preserve session identity** boundary. The required behavior is: Keep the same session identity across related A2A follow-up messages.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: discover the existing Agent, preserve one AgentSession uid across related turns, honor its advertised message or task capabilities, and keep retries idempotent. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal creates a new session for every follow-up, invents an unsupported response kind, and treats a transport acknowledgement as completed task output. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires the selected Agent identity, stable session, response envelope or terminal task state, and bounded failure evidence.
