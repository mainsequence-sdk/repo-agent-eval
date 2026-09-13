The evidence analysis should center **Preserve environment and session provenance**: Tie deployed Agent sessions to the intended repository revision, environment, and runtime identity.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: audit readiness, keep repository instructions and skills durable, separate source and runtime Agent cards, use a stable non-mutating runtime command, and declare automatic deployment through the workflow.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: prepared source revision, validated workflow target, backend image build, release revision, DeploymentRun, Agent reachability, and session provenance. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
