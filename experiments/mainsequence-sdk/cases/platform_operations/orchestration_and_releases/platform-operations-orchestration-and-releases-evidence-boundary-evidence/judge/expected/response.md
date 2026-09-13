The evidence analysis should center **Establish deployment proof**: Require the full evidence chain for release, health, execution, and expected output behavior.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: separate workflow, Job, schedule, JobRun, image, artifact, resource, release, revision, and DeploymentRun identities and require the full proof chain.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: workflow validation, exact image, JobRun logs/usage, artifact identity, release revision, DeploymentRun, health, and representative output. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
