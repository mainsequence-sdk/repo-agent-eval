Read `AGENTS.md` and the maintenance skill, then inspect before mutation:

```bash
mainsequence code-repository current --debug --json
mainsequence code-repository sdk-status --path . --json
git status --short
mainsequence code-repository update-sdk --path . --dry-run
```

Require an attached registered Git branch with resolved repository/branch
context, separate pre-existing user changes, and review the SDK update preview.
Then, because the requested outcome includes the update and managed context
refresh, run:

```bash
mainsequence code-repository update-sdk --path .
mainsequence code-repository update-agent-skills --path .
mainsequence code-repository update AGENTS.md --path .
```

Verify the installed/locked SDK state and
`.agents/skills/mainsequence/PINNED_FROM.txt`: SDK version must match and the
platform manifest/ontology/resource hashes must be present. Confirm custom
skills outside the managed directory are unchanged. Because the refresh may
replace the currently loaded maintenance instructions, re-read the refreshed
`code_repository_maintenance/SKILL.md` before another maintenance routine.
Run only the validations required by `AGENTS.md` and the changed files.

Report repository path, installed SDK version, changed files/generated state,
validation and results, and remaining gaps. Leave changes local. Do not invoke
`code-repository sync`, `git commit`, tag, or push because publication was not
requested.
