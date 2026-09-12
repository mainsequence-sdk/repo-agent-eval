Put each shared recurring declaration in a direct `.yaml` or `.yml` child of
`.mainsequence/workflows/`, for example
`.mainsequence/workflows/daily-metrics.yaml`. Do not use a nested directory or
the unsupported `scheduled_jobs.yaml` convention.

Resolve the current Git-derived CodeRepositoryBranch, retrieve its current
backend workflow template, and author only fields/resource kinds advertised by
that template, including its `api_version`, workflow name, execution target,
schedule, and image strategy. The backend owns parsing, defaults, permissions,
validation, and application; repository code must not reproduce the parser or
construct an interpreted deployment payload. Submit the proposed path and
content to the branch's backend validation endpoint before commit.

Every resulting Job must resolve to one exact image. Manual declarations must
pin an exact ready image; automatic deployment must leave initial image choice
to the backend's synchronized-commit flow and cannot refer to unpushed changes.

After review and push, inspect the repository-event action and resulting
deployment runs because workflow files apply independently and an invalid file
does not block a valid sibling. Then inspect the created Job, schedule or
trigger a JobRun as appropriate, and inspect terminal status and owner-scoped
logs. A successful Git push proves only that Git accepted refs; it does not
prove workflow application, deployment, scheduling, or execution.
