`POLYGON_API_KEY` is sensitive and therefore belongs in a `Secret`, never a
`Constant`. Its unique Environment-level configuration identity is its name.
Derive the Environment from the registered Git branch; do not ask for or store
an Environment or CodeRepositoryBranch UID.

Resolve before creating. Because the current CLI has no public secret
get/detail command, use the name-filtered list:

```bash
mainsequence secrets list --filter name=POLYGON_API_KEY
```

If exactly one Secret exists, reconcile that object. If none exists, create it
once through an authorized secure input path without printing or returning its
value. If duplicates or an ambiguous result appear, stop rather than choosing
arbitrarily. Never implement “ensure” as unconditional create.

The supplied user is a maintainer, so add only the missing `edit` grant using
public user UID `0ea85127-7bb7-4db2-a519-ad1e187c5ee2`. Preserve every unrelated
view/edit and team grant; do not replace the access list. After an authorized
mutation, resolve the Secret again by name and inspect its access state to
confirm the intended UID has edit while other grants remain. Verify the
authorized workload can resolve the secret without logging its value. The plan
does not establish that the Secret or grant currently exists.
