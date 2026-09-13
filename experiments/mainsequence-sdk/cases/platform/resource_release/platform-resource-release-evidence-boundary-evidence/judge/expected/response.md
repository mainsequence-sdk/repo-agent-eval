The evidence analysis should center **Protect release deletion**: Distinguish stopping automation, retiring a release, and deleting underlying resources.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: select the correct release kind, preserve revision lifecycle, apply direct-versus-workflow image rules, configure CORS and redeployment, and observe DeploymentRuns before claiming success.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: resource and release kind, immutable revision, image/build owner, DeploymentRun and logs, health, contract behavior, and safe retirement state. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
