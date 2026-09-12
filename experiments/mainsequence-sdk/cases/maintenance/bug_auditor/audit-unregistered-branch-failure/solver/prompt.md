# Task

Audit this repository state:

```text
path: /workspace/analytics
git remote: git@github.com:example/analytics.git
git branch: feature/new-risk
git status --short: clean
local tests: 42 passed

$ mainsequence code-repository current --debug --json
{
  "code_repository_uid": "repo-10",
  "git_branch": "feature/new-risk",
  "code_repository_branch_uid": null,
  "code_repository_branch_status": "unregistered"
}

$ mainsequence migrations current --provider analytics.migrations:migration
Error: current Git branch is not registered as a CodeRepositoryBranch
exit code: 2
```

The implementation is claimed “fully finished because tests pass,” and a
teammate proposes setting `CODE_REPOSITORY_BRANCH_UID` in `.env`. Produce the
Bug Auditor output shape, including the real blocker and a safe next action.
Do not change state.
