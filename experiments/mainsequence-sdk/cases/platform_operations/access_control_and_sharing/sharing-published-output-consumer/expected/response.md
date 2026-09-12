The shareable consumption boundary is the updater's published
`TimeIndexMetaTable`, not the updater process or repository code. Resolve the
exact published table first and grant `view` to the existing
`market-research` team using its public UUID
`4a8d8dd9-6172-4547-8154-540aa77db2ac`. Do not grant `edit`: the analysts need
to read/consume only. A team grant is appropriate because the same access
pattern covers twelve current and future team members.

Before mutation, inspect the exact table's current access state, confirm the
UID belongs to the intended team, and preserve all unrelated direct and team
grants. Apply only the missing team-view grant through the resource's supported
sharing operation. Do not use a numeric database ID and do not infer that team
membership makes an analyst a team administrator.

After an authorized change, re-read access state on that exact
`TimeIndexMetaTable` and test the intended read path as an appropriate team
member. Verify that consumption succeeds and that edit/maintenance permission
was not added. The supplied prompt proves the desired policy only; it does not
prove the resource has been resolved, the grant applied, or effective access
verified. Scheduling and producer implementation remain outside this task.
