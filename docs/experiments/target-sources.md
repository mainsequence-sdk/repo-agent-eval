# Target Source and Snapshot Workflow

The target is configured directly in `workspace.yaml`:

```yaml
evaluation:
  repository:
    url: https://github.com/example/project
    ref: v1.2.3
  instructions:
    global:
      - AGENTS.md
    skills:
      directory: .agents/skills
  cases:
    directory: cases
    coverage:
      catalog: cases/coverage.yaml
      minimum_cases_per_skill: 8
      minimum_leakage_groups_per_skill: 4
```

An exact file list is the only alternative:

```yaml
skills:
  files:
    - .agents/skills/one/SKILL.md
    - .agents/skills/two/SKILL.md
```

The target above is the solver's normative instruction source. Codex case
authoring may additionally use independently pinned tutorials, applications, and
contract repositories:

```yaml
evaluation:
  case_authoring:
    sources:
      - id: tutorial
        role: illustrative
        repository:
          url: https://github.com/example/tutorial
          ref: v1.4.0
        include:
          - README.md
          - docs/**
          - src/**
```

`id` and `role` are explicit lowercase identifiers. `include` is a non-empty
list of safe repository-relative globs. It controls which locked files can be
selected as authoring evidence; it does not weaken snapshot integrity.

`validate`, `inspect`, and `run` automatically:

1. resolve every authored tag to one full commit;
2. materialize only those revisions without submodules or Git LFS;
3. read every configured global file;
4. recursively discover `SKILL.md` under the configured directory, or use the
   exact file list, and catalog those instructions separately;
5. reject links, traversal, missing files, empty discovery, and duplicate skill
   ids;
6. publish every immutable repository snapshot under the external data root;
7. expose selected reference files to Codex under
   `references/<source-id>/...`, while preserving target paths as-is;
8. generate a lock with requested ref, resolved commit, repository path,
   normalized snapshot path, size, and content hash for every file;
9. compute one authoring source-set hash over the target and all references;
10. reuse and verify snapshots without network on later runs.

There is no target registry, snapshot registry, compatibility map, authored
count assertion, locator filename, or fallback path probing. A hidden directory
such as `.agents/skills` works only when the manifest explicitly selects it.

After instruction discovery, Codex decomposes every complete selected skill
into `cases/coverage.yaml`. Scored-run readiness requires a complete catalog,
the configured primary-case depth for every capability, and the numeric case
and leakage-group floors. Supporting capability tags do not earn primary
coverage.

The default external location is
`~/ms_agent_eval/<workspace-id>/operations/snapshots`.
Set `workspace.data_root` to another path outside the Git workspace when needed.

For a private GitHub HTTPS URL, resolution tries anonymous access first and
then the machine's configured non-interactive Git credential helper. The
credential is passed to Git through an ephemeral process environment; it is
never written to `workspace.yaml`, snapshot locks, manifests, or command
arguments. If no helper can supply access, source resolution fails before any
model call.
