The evidence analysis should center **Manage environment lifecycle transitions**: Handle creation, readiness, transition, and decommissioning with explicit evidence and safeguards.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: treat environments as organization partitions, verify branch compatibility, require explicit human selection, derive runtime scope from the target, and promote code separately from configuration and data.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: organization/environment identities, branch compatibility, isolated resource resolution, promotion records, and lifecycle state. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
