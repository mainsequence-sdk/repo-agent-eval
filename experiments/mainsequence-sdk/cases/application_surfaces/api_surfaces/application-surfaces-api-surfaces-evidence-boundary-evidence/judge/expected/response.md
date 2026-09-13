The evidence analysis should center **Establish release evidence**: Distinguish declaration, release, health, data proof, and deployed behavior evidence.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: use the authoritative frontend contract, accept identity only from platform-injected request state, keep transport thin, and separate release health from payload conformance.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: schema and fixture conformance, trusted-request identity handling, release state, health, and one representative data behavior. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
