Classify endpoints independently: `/health` is operational, while
`/market-bars` is Command Center contract-bearing. Pin a Command Center SDK
branch/tag/commit and record the resolved commit. At that commit, select the
exact contract ID from `contracts/manifest.json`, then load its referenced
draft-2020-12 schema, all indexed valid and invalid fixtures, and its contract
skill when present. Pydantic and OpenAPI are implementation aids, not the wire
authority.

Local evidence for `/market-bars` must compile that schema, accept every valid
fixture, reject every invalid fixture, and validate representative JSON-serialized
route responses. It should also cover request validation, injected request
identity, authorization, declared errors, and secret/runtime-object redaction.
Record the contract ID, schema `$id`, and resolved Command Center SDK commit.
Exercise the actual frontend flow locally using the contract-defined transport.

A 200 from `/health` proves only that the health route responded. Creating a
release proves only that a release record exists. Neither proves deployment,
the selected code/image, contract conformance, data correctness, nor frontend
consumption. After release, verify the deployed route's authentication and
health, execute representative `/market-bars` requests, validate returned JSON
against the same pinned schema, exercise the consuming frontend, and inspect
runtime logs. If automatic deployment is enabled, also verify the deployment
run selected the expected commit, resource path, and image.
