The evidence analysis should center **Respect role and behavior boundaries**: Keep caller, target Agent, task, response, and capability responsibilities distinct.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: discover the existing Agent, preserve one AgentSession uid across related turns, honor its advertised message or task capabilities, and keep retries idempotent.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: the selected Agent identity, stable session, response envelope or terminal task state, and bounded failure evidence. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
