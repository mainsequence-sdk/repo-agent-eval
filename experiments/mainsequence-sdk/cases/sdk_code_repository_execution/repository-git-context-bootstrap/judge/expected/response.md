First define success, for example: repository-relevant updater tests pass
locally; an authorized validation run uses an explicit isolation namespace;
the intended exact Git commit is available to the registered branch runtime;
and the resulting published `TimeIndexMetaTable` contains the expected rows and
time-index contract, with concrete run/log/table evidence.

Read `AGENTS.md`, current repository docs, and the updater skill. From the
repository root run the read-only context check:

```bash
mainsequence code-repository current --debug --json
```

Require its Git-derived remote/repository, `git_branch=feature/prices`, exact
commit context, `code_repository_branch_status=resolved`, and a nonempty
resolved branch UID before live branch-owned operations. Git remote, attached
branch, and HEAD are the source identity. Authentication grants access but does
not select source context. Never request or persist Environment,
CodeRepository, or branch UID selectors in `.env`. If the branch is
unregistered, local development and tests may continue, but platform-derived
MetaTable, Job, image, release, or live validation operations are blocked until
that actual Git branch is registered and a fresh process resolves it.

Route updater configuration, dependency, incremental logic, storage contract,
and namespace-first validation to
`data_publishing/time_index_table_updates`. Return here only to verify the
agreed repository context and evidence boundary; this skill does not absorb the
updater implementation.
