Do not rerun the entire sync. It may calculate another patch bump, create a
second commit, or collide with the existing tag. Preserve current state and
inspect:

```bash
git status --short
git log -1 --decorate --oneline
git tag --points-at HEAD
git branch -vv
git ls-remote --heads origin <current-branch>
git ls-remote --tags origin refs/tags/v1.4.3
```

Also inspect the current package version and confirm `uv.lock`/exported
requirements match the committed version. Re-run `mainsequence
code-repository current --debug --json` to confirm the attached branch still
resolves. These observations determine whether the local commit and tag exist,
whether the branch commit reached the remote despite the client error, whether
the remote tag exists, and whether anything remains uncommitted.

Choose the smallest documented recovery from the actual state. If both refs
are already remote, do not push or sync again; verify the remote commit/tag and
then separately check backend reconciliation if required. If neither ref is
remote and local state exactly matches the completed sync stages, diagnose the
network/auth failure and resume only the required publication step through the
canonical supported workflow. If branch/tag state diverges or is ambiguous,
stop and report it rather than rewriting history or inventing a tag.

The failed command proves local mutation stages occurred according to its
output, but it does not prove either remote ref or backend webhook
reconciliation succeeded.
