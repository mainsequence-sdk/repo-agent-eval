# ADR-002: Isolated Codex CLI Harness Execution

- Status: Accepted
- Date: 2026-09-12
- Decision owners: MS Agent Eval maintainers
- Scope: Isolated local Codex execution for model-driven authoring, solving,
  judge-calibration, and judging stages
- Refines: [ADR-001: Portable Staged Execution](001-portable-staged-execution.md)

## Context

ADR-001 establishes a portable packet/result contract, but did not define the
security boundary around a local Codex process.

A benchmark result is invalid if the executing agent can inspect information
outside its assigned packet. Examples include:

- other repositories on the host;
- expected answers, rubrics, or judge feedback hidden from a solver;
- solver responses hidden from a case author;
- test or challenge cases hidden from an optimizer;
- previous run results or prior Codex sessions;
- credentials or environment variables; and
- repository instructions that were not intentionally admitted as experiment
  inputs.

`codex exec --sandbox read-only` is not a read-confinement guarantee. Read-only
mode controls mutation and command approval, but it does not mean that the
current working directory is the only readable directory. OpenAI's
[sandbox documentation](https://learn.chatgpt.com/docs/sandboxing) describes
read-only mode in terms of inspecting files while preventing changes. The
[GitHub Action documentation](https://learn.chatgpt.com/docs/github-action)
also warns that read-only sandboxing alone must not be relied upon to protect
secrets from repository-controlled code.

Codex permission profiles can deny the filesystem root and selectively grant
read or write access to workspace roots. OpenAI's
[permission documentation](https://learn.chatgpt.com/docs/permissions)
documents `:root`, `:minimal`, `:workspace_roots`, `:tmpdir`, and `:slash_tmp`,
including rejection of `../` traversal out of a workspace root. These controls
are necessary defense in depth, but a serious evaluation must also ensure that
forbidden data is absent from the execution environment.

There is an additional network constraint. The Codex CLI needs a control-plane
connection to its model provider, but model-requested shell commands must not
have arbitrary network access. A blanket `docker run --network none` would
also prevent the CLI itself from reaching the model. The pipeline must separate
provider transport from agent-tool network permissions.

Finally, `AGENTS.md`, `AGENTS.override.md`, and configured fallback instruction
files are executable context from the agent's perspective. Files copied from a
candidate repository can therefore contaminate an author or judge even when
filesystem isolation is otherwise correct.

## Decision

Add an isolated `codex_cli` harness adapter. For benchmark-grade execution it
will run each stage attempt in a fresh, disposable container assembled from
only the immutable inputs permitted by that stage packet.

The configuration introduced by ADR-001 is refined as follows:

```yaml
executors:
  local-codex:
    type: harness
    adapter: codex_cli
    version: "0.153.1"
    options:
      image: "<codex-runner-image>@sha256:<64-hex-digest>"
      authentication: chatgpt_subscription
      permission_profile: evaluator
      instruction_policy: controlled
      control_plane:
        docker_network: agent-eval-codex-control
        https_proxy: http://codex-egress-proxy:3128
        policy_id: openai-control-plane-v1
      resources:
        cpus: 2
        memory_mb: 4096
        pids: 256
        maximum_output_bytes: 8388608
```

There is no `codex_sdk` compatibility adapter. Interactive/manual execution uses
the portable `artifact_exchange` lifecycle; isolated automatic execution uses
`codex_cli` explicitly.

The independent-execution boundary is:

```text
Host coordinator and external artifact store
                 │
                 │ materialize allowlisted, hash-verified packet inputs
                 ▼
        ┌───────────────────────────────┐
        │ fresh disposable container    │
        │                               │
        │ /workspace                    │
        │   permitted stage inputs only │
        │                               │
        │ isolated CODEX_HOME           │
        │ no prior sessions             │
        │ no host home                  │
        │ no other repositories         │
        │ no result store               │
        │                               │
        │ Codex CLI                     │
        │   ephemeral session           │
        │   restricted FS profile       │
        │   web search disabled         │
        │                               │
        │ agent shell ────────────────X │ arbitrary network
        └───────────────┬───────────────┘
                        │ Codex control-plane traffic only
                        ▼
               restricted egress proxy
                        │
                        ▼
                 configured provider

JSONL stdout ──► coordinator validation ──► external artifact store
container and temporary volumes destroyed
```

The container is the primary boundary that makes unrelated information
unavailable. The Codex permission profile is a second boundary that limits what
agent-requested commands can read or change inside the container. Stage packet
construction is a third boundary that prevents prohibited information from
being copied into the container in the first place.

## Security properties

A benchmark-grade `codex_cli` attempt must satisfy all of the following:

| Property | Required behavior |
| --- | --- |
| Fresh model session | Invoke `codex exec --ephemeral`; never resume a previous thread |
| Fresh compute | Create a new container and temporary volumes for every attempt |
| Minimal input | Materialize only artifacts referenced by the locked stage packet |
| Host isolation | Do not mount the user's home, project root, other repositories, or external result store |
| Read confinement | Deny filesystem root by default and allow only declared workspace/runtime paths |
| Write confinement | Make the container root read-only; allow stage-specific disposable write locations only |
| Tool network denial | Deny network to agent-invoked commands and disable Codex web search |
| Provider connectivity | Route only Codex model-control traffic through an allowlisted proxy |
| Credential isolation | Do not expose provider secrets in the workspace, inherited shell environment, logs, or result |
| Instruction control | Admit, exclude, or quarantine repository instruction files according to an explicit stage policy |
| Deterministic identity | Pin the container digest, Codex CLI version, model, reasoning settings, config, schema, and packet hashes |
| External persistence | Capture stdout and selected artifacts through the coordinator; never mount historical results into the container |
| Guaranteed cleanup | Destroy the container and all attempt-local writable volumes after submission or failure |

`--ephemeral` prevents persistence of the session rollout; it does not provide
filesystem isolation. `--json` provides a JSONL event stream and
`--output-schema` constrains the final response. These behaviors are documented
in [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

## Threat model

### Threats addressed

The design must prevent or detect:

- direct reads of other host directories or repositories;
- `..` traversal, absolute-path access, and symlink escape from the packet;
- discovery of expected answers, hidden test cases, other candidates, or prior
  results;
- discovery of prior Codex sessions or a host `CODEX_HOME`;
- secret extraction through files, environment variables, `/proc`, logs, or
  shell startup files;
- network retrieval of solutions or network exfiltration by agent tools;
- Docker socket or host-runtime access;
- persistence through shared temporary directories, caches, or writable mounts;
- prompt injection from undeclared `AGENTS.md` or fallback instruction files;
- output that does not match the packet's declared schema; and
- substitution of an unpinned CLI, container, model, or permission profile.

### Threats not claimed to be solved

This ADR does not claim protection against:

- a compromised host kernel, container runtime, or runner administrator;
- provider-side retention or compromise;
- hardware side channels;
- flaws in the selected model itself; or
- benchmark leakage that already exists in model training data.

Those limitations must be stated in any report that describes a run as
isolated.

## Stage-specific information views

The coordinator must build a different filesystem projection for every stage.
Mounting an entire experiment directory and asking the model not to inspect
hidden files is prohibited.

| Stage | Permitted input view | Explicitly excluded |
| --- | --- | --- |
| `author` | Authoring policy, capability catalog, selected immutable target/reference evidence, campaign request, output schema | Solver outputs, judge outputs, optimizer traces, held-out results |
| `calibrate_judge` | Assigned calibration fixtures, their cases, rubrics, expected evidence, output schema | Real solver responses and unrelated fixtures |
| `solve` | Case prompt, declared global context, selected skill context, permitted repository snapshot, runtime/test files | Rubric, expected answer, judge prompt, judge feedback, other split data |
| `judge` | Assigned case prompt, rubric, expected evidence, locked candidate response, permitted supporting snapshot | Other candidates, aggregate scores, previous judge decisions, unrelated cases |
| `optimize` | Train/validation inputs and explicitly admitted prior results | Test and challenge inputs, answers, responses, and feedback |
| `final_evaluate` | Published optimizer artifact plus the assigned untouched case view | Other held-out cases and any post-evaluation feedback |

An artifact referenced by an allowed directory is not automatically safe. The
materializer must create a new tree from the allowlist rather than bind-mount a
parent directory containing both allowed and hidden data.

### Canonical case package versus runtime views

The versioned case package is coordinator-owned and uses schema version 5:

```text
cases/<skill>/<case>/
├── case.yaml                         coordinator metadata and provenance
├── solver/
│   ├── prompt.md                     public task
│   └── attachments/                  optional public fixtures
└── judge/
    ├── rubric.yaml                   semantic criteria plus private threshold
    └── expected/
        ├── response.md               judge reference evidence
        └── artifacts/                optional judge reference artifacts
```

No model receives this tree directly. The coordinator creates a fresh
projection for each invocation:

```text
canonical case             solver view                 judge view
──────────────             ───────────                 ──────────
case.yaml          X       input/task.md               input/task.md
solver/prompt.md   ──────► input/task.md               input/candidate.md
solver/attachments ──────► input/attachments/          input/judge/rubric.json
judge/rubric.yaml  X                                  input/judge/expected-*
judge/expected/*   X
```

The judge view contains rubric criteria and weights but not `passing_score`.
For judge calibration, the model sees one candidate fixture at a time and never
sees its reference label or accepted score range. The coordinator applies
thresholds, calibration labels, ordering checks, and final pass/fail decisions
after schema-valid model output is returned.

## Repository instruction policy

Every Codex-backed stage must declare one of these policies:

```yaml
instruction_policy:
  mode: controlled       # controlled | none
  controlled_files:
    - evaluator/AGENTS.md
  fallback_filenames: []
```

- `controlled` includes only runner-owned, content-addressed instruction files.
  Candidate `AGENTS.md`, `AGENTS.override.md`, and fallback instruction files
  are excluded from automatic discovery. This is the default for `author`,
  `calibrate_judge`, and `judge`.
- `none` disables project instruction discovery and includes no instruction
  files beyond the explicit stage prompt.

Candidate-controlled instruction discovery is intentionally unsupported. If a
case needs to analyze an instruction file, author it as a solver attachment
under a non-discoverable name and make the task explicitly quote or inspect it.

Exclusion must cover nested instruction files and all fallback names configured
for the pinned Codex CLI. A candidate instruction required as evidence may be
copied under a non-discoverable name and quoted by the stage prompt; it must not
silently become controlling context.

The chosen mode, discovered instruction inventory, and hashes must be recorded
in packet identity and result provenance.

## Filesystem permission profile

The adapter generates an isolated `CODEX_HOME/config.toml` for the attempt. An
illustrative read-only evaluator profile is:

```toml
default_permissions = "evaluator"
approval_policy = "never"
web_search = "disabled"
allow_login_shell = false

[permissions.evaluator.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"

[permissions.evaluator.filesystem.":workspace_roots"]
"." = "read"

[permissions.evaluator.network]
enabled = false

[shell_environment_policy]
inherit = "none"
ignore_default_excludes = false

[shell_environment_policy.set]
PATH = "/usr/local/bin:/usr/bin:/bin"
LANG = "C.UTF-8"
```

The implementation must validate this generated configuration against the
pinned CLI's configuration schema during image build and again during runner
preflight. If the pinned version does not support the required permission
semantics, execution fails closed.

Do not also pass `--sandbox read-only`: Codex configuration does not permit the
legacy `sandbox_mode` and a permission profile to define competing policies.
The adapter selects the named permission profile through its isolated config.

Workspace access is stage-specific:

- `author`, `calibrate_judge`, and `judge` receive read-only workspaces and
  return their semantic result through stdout;
- a read-only response-only `solve` case uses the same profile;
- a coding `solve` or local `optimize` stage may receive `"." = "write"`, but
  only inside a disposable workspace populated from immutable input; and
- no stage receives write access to the input bundle, host repository, or
  external artifact store.

A writable stage is still read-confined. Its output is extracted by the
coordinator from explicitly declared paths before the container is destroyed.

## Container boundary

The OCI runner must use the equivalent of these controls:

- exact image digest, never a mutable tag alone;
- one new container per stage attempt;
- non-root user;
- read-only container root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges` enabled;
- default or stricter seccomp and AppArmor/SELinux profile;
- no privileged mode, host PID namespace, host IPC namespace, host devices, or
  Docker/container-runtime socket;
- bounded CPU, memory, PIDs, output bytes, and wall-clock time;
- no bind mount of the host home, source workspace, `.git`, SSH agent, credential
  directories, `/tmp`, or result store;
- isolated tmpfs runtime directories whose contents die with the attempt; and
- a read-only input bundle plus a disposable workspace when writes are needed.

For a read-only judge, the conceptual container invocation is:

```text
container run --rm
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --user <unprivileged-uid>
  --mount <packet-input>:/workspace:ro
  --tmpfs /run/codex
  --pids-limit <limit>
  --memory <limit>
  --cpus <limit>
  --network <control-plane-only-network>
  <image>@sha256:<digest>
```

This is intentionally conceptual rather than a copy-paste command. The actual
implementation must support Docker first through a container-runtime interface
that can later support another OCI runtime without weakening the contract.

## Network separation

The container has two logically distinct network consumers:

1. the trusted Codex CLI needs to exchange requests with the configured model
   provider; and
2. untrusted agent-requested tools must have no network.

The implementation must enforce both layers:

- the Codex permission profile sets agent tool network access to `false`;
- Codex web search is disabled;
- no browser, MCP server, connector, plugin, or external tool is registered for
  the attempt unless the packet explicitly defines and hashes it;
- the container network can reach only a runner-controlled egress proxy;
- the proxy allowlists the exact model-control endpoints required for the
  executor and rejects all other destinations and methods; and
- proxy policy and logs must not expose request bodies or credentials in run
  artifacts.

If a provider cannot be reached through this restricted control plane, the
benchmark run must fail. The adapter must not silently fall back to unrestricted
host networking.

`--network none` is valid only when the model control plane is provided without
container networking. It must not be documented as the default while the Codex
CLI itself still needs remote provider access.

## Authentication and environment

Inference must use the operator's Codex/ChatGPT subscription. API exchanges
performed by the Codex sign-in flow are allowed for authentication, but the
adapter does not accept an OpenAI API key and must never fall back to API-billed
model consumption.

Model execution still requires HTTPS access to the Codex service and reports
token counts for limits, provenance, and experiment budgets. These are
subscription-backed model requests, not OpenAI API Platform token charges. The
CLI's API-key mode cannot be used as an authentication-only mechanism because
it also selects API-billed inference; therefore it is rejected rather than
accepted and relied upon by policy alone.

The operator provisions a dedicated credential store outside every repository
and experiment data root:

```bash
ms-agent-eval codex login
ms-agent-eval codex status
```

The login command uses Codex device authentication with
`cli_auth_credentials_store = "file"`. The coordinator validates that the
result is marked `chatgpt_subscription`, refuses symlinked or group/world
readable credentials, and copies only `auth.json` into a fresh attempt-local
`CODEX_HOME`. It never mounts the host user's general `~/.codex`, previous
sessions, configuration, skills, or caches. The attempt copy is destroyed with
the staging directory.

The auth file is a secret even though inference is subscription-backed. It is
not copied into `/workspace`, passed on the command line, included in result
provenance, or persisted in logs. The generated permission profile denies
filesystem root access, and `shell_environment_policy.inherit = "none"`
prevents agent shell commands from inheriting host or coordinator credentials.

Failure of any credential-isolation probe blocks release of the adapter.

## CLI execution contract

The container entrypoint performs one non-interactive invocation equivalent to:

```bash
codex exec \
  --ephemeral \
  --json \
  --output-schema /workspace/control/output.schema.json \
  --output-last-message /run/codex/result.json \
  --model "<packet-model>" \
  -C /workspace \
  -
```

The coordinator sends the fully rendered, hash-verified stage instruction on
stdin. It captures stdout and stderr separately with byte limits. The adapter
must not interpolate packet text into a shell command.

The coordinator treats JSONL as an untrusted event stream:

1. validate event framing and maximum size;
2. select the single final structured output;
3. validate it against the packet's output schema;
4. verify stage-specific invariants and declared artifact paths;
5. hash accepted outputs;
6. build the portable stage result defined by ADR-001; and
7. only then persist the result outside the container.

An exit code of zero or a valid Codex final event does not complete a stage by
itself. Schema validation, packet identity, artifact validation, and coordinator
acceptance remain mandatory.

Raw JSONL, stderr, and tool transcripts may contain source code or model input.
They are external run artifacts subject to retention and redaction policy; they
must not be committed to the framework or experiment repository.

## Input materialization

Before container creation, the coordinator builds a content-addressed staging
tree from the packet manifest. Materialization must:

- resolve every artifact by its declared content hash;
- reject paths containing traversal or absolute destinations;
- reject symlinks that resolve outside the artifact root;
- reject device nodes, sockets, FIFOs, setuid/setgid bits, and unsupported file
  types;
- normalize or reject hard links;
- enforce file-count, individual-size, and total-size limits;
- remove undeclared instruction files according to `instruction_policy`;
- avoid bind-mounting a broader parent directory; and
- produce a final manifest of path, mode, size, and hash for provenance.

The external data root can contain source snapshots and run records, but it is
never itself mounted. Only the newly materialized attempt view is exposed.

## Output extraction

Response-only stages return their result exclusively through captured stdout.
They receive no writable host mount.

Stages that legitimately create files declare an output allowlist in the
packet. After Codex exits, the coordinator extracts those paths from the stopped
container or disposable volume, applies the same path/type/size validation used
for inputs, hashes the files, and only then writes them to the external artifact
store. The agent cannot browse that store or previous output during execution.

Partial output from a timed-out, killed, or invalid attempt is quarantined and
is never promoted as a completed stage result.

## Provenance

Every `codex_cli` stage result must record at least:

- packet ID and attempt ID;
- stage kind and stage-specific input-view manifest hash;
- selected instruction policy and instruction-file hashes;
- exact model ID and reasoning effort;
- exact Codex CLI version;
- container image name and digest;
- generated Codex configuration hash and permission-profile name;
- output-schema hash;
- container runtime name and version;
- network-policy identity and egress-proxy policy version;
- workspace access mode;
- resource and wall-clock limits;
- start/end timestamps and exit classification;
- structured-output and extracted-artifact hashes; and
- whether every isolation preflight and cleanup check passed.

Credentials, raw authorization headers, and unredacted process environments are
never provenance.

## Failure behavior

The adapter fails closed when:

- any packet input hash fails;
- the materialized view contains a forbidden file or escape;
- the CLI/image/config version is not the pinned identity;
- a required permission cannot be enforced;
- arbitrary egress is possible;
- a credential-isolation probe fails;
- the container exceeds a resource budget;
- output is missing, oversized, malformed, or schema-invalid;
- undeclared output artifacts are requested; or
- cleanup cannot be confirmed.

There is no fallback to host execution, a reusable Codex session, a less strict
permission profile, or unrestricted networking. Retrying creates a new attempt,
container, workspace, and credential while retaining lineage to the same packet.

## Implementation status

### 1. Extend executor configuration

Implemented typed `codex_cli` configuration with:

- pinned CLI version and container digest;
- model and reasoning settings supplied by the stage assignment;
- stage-specific workspace access;
- named permission profile;
- instruction policy;
- control-plane proxy policy;
- CPU, memory, PID, output-size, and wall-clock budgets; and
- explicit `chatgpt_subscription` authentication.

Workspace validation must reject `codex_cli` benchmark executors without a
container digest, restricted network policy, explicit instruction policy, or
resource limits.

### 2. Build the packet-view materializer

Implemented the minimal filesystem projection described above. Its output is an
immutable manifest plus a staging directory that contains no parent experiment
tree, result store, credential, or undeclared instruction file.

### 3. Build and pin the runner image — deployment responsibility

Create a minimal Python 3.12+ runner image containing the exact Codex CLI and
only the system tools required by supported cases. Publish it by digest. Generate
the attempt-local Codex configuration from typed settings and validate it during
the image test suite.

Different toolchains may require different pinned runner profiles. Selecting a
profile is part of packet identity; installing dependencies from the network at
runtime is prohibited unless a future ADR defines a controlled dependency
mirror.

### 4. Implement the OCI runtime boundary — implemented for Docker

Implement container creation, input mounting, disposable writable workspaces,
limits, control-plane-only networking, output capture, artifact extraction, and
guaranteed cleanup behind a narrow runtime interface. Support Docker first.

### 5. Implement Codex process execution — implemented

Run exactly one `codex exec` per attempt with stdin prompt delivery,
`--ephemeral`, JSONL output, and the packet schema. Do not call `resume`, mount a
host session store, or share an attempt-local `CODEX_HOME`.

### 6. Add subscription authentication — implemented

Provision a dedicated subscription login with `ms-agent-eval codex login`,
validate it before execution, copy only the required auth file into the
attempt-local `CODEX_HOME`, and never accept an API key for this adapter.

### 7. Integrate with the staged coordinator — implemented

Register `codex_cli` through the same prepare, claim, execute, submit, validate,
and persist lifecycle as every ADR-001 executor. The core coordinator receives a
portable stage result and must not depend on Codex-specific event types.

### 8. Persist security provenance — implemented

Extend result provenance with the container, config, instruction, network, and
preflight identities listed above. Store logs and transcripts only in the
external run directory under the configured retention policy.

### 9. Migrate configuration and documentation — implemented

Use `artifact_exchange` for interactive execution and explicit `adapter:
codex_cli` for isolated automatic execution. There is no SDK compatibility
adapter or silent migration.

### 10. Gate release on isolation tests

Do not mark this ADR Accepted or call the executor benchmark-grade until all
acceptance tests pass on every supported runner platform.

## Required acceptance tests

The implementation is complete only when automated tests prove:

1. a sentinel elsewhere in the host workspace cannot be listed, opened, or
   found;
2. another repository, external result directory, host home, and host
   `CODEX_HOME` are absent;
3. `..`, absolute paths, symlink escapes, hard-link tricks, and special files
   cannot escape the materialized view;
4. host and shared temporary directories are unavailable;
5. agent shell commands cannot reach the internet, local network, metadata
   endpoints, or the model-control proxy as a general-purpose HTTP proxy;
6. the Codex CLI can still reach only its configured model control plane;
7. web search, MCP servers, connectors, plugins, and undeclared external tools
   are disabled;
8. provider credentials cannot be obtained from the workspace, environment,
   command line, auth files, logs, process inspection, or output;
9. a malicious candidate `AGENTS.md` cannot influence a `controlled` or `none`
   judge, while `candidate` mode includes and hashes it intentionally;
10. a solve packet cannot access its rubric, expected answer, judge feedback,
    another split, or another candidate;
11. an optimize packet cannot access test or challenge data;
12. read-only stages cannot mutate the workspace, and writable stages can
    mutate only their disposable workspace;
13. only declared, validated output paths can be extracted;
14. malformed, oversized, duplicated, or schema-invalid final output is
    rejected;
15. timeouts and crashes create failed attempts without promoting partial
    output;
16. no session, cache, workspace mutation, or temporary volume survives the
    attempt; and
17. the recorded hashes and versions are sufficient to identify the exact
    executor environment used.

The test suite must include an intentionally adversarial fixture that asks
Codex to locate answer files, read parent directories, inspect secrets, use the
network, and override the evaluator through repository instructions. A polite
prompt is not an isolation test.

## Consequences

### Benefits

- Local Codex stages can produce independently auditable benchmark artifacts.
- Hidden-data boundaries become enforced by construction rather than by prompt.
- The same harness can safely pause after remote solving and later run Codex
  judging against locked responses.
- Stage-specific instruction and data visibility becomes explicit provenance.
- The design supports read-only judging and writable coding tasks without
  exposing host data.

### Costs

- Container image maintenance and platform testing are required.
- Restricted provider egress and credential brokering add infrastructure.
- Cold container startup makes local harness execution slower.
- Toolchains must be prebuilt into pinned images instead of downloaded ad hoc.
- Raw Codex event streams and extracted artifacts require strict size,
  retention, and redaction handling.

### Residual risk

Container isolation reduces host-data leakage but does not make the model or
provider trusted. Results still depend on the pinned model, evaluator prompt,
rubric quality, calibration suite, and absence of upstream benchmark leakage.

## Alternatives rejected

### Host `codex exec --sandbox read-only`

Rejected because read-only is a mutation policy, not proof that unrelated host
files are unreadable.

### Permission profile without a disposable container

Rejected as the sole benchmark boundary. It is valuable defense in depth, but
forbidden host data still exists and a policy/configuration defect has a much
larger blast radius.

### Container with unrestricted networking

Rejected because the agent could retrieve external answers or exfiltrate
evaluation data through shell tools.

### Container with `--network none`

Rejected as the default because an in-container Codex CLI still needs to reach
its model provider. It is acceptable only with a separate, proven control-plane
transport.

### Mount the complete experiment data root read-only

Rejected because read-only hidden answers are still readable. The input view
must contain only permitted packet artifacts.

### Prompt-only instructions not to inspect other files

Rejected because benchmark independence must be an enforced system property,
not voluntary model behavior.

## Deployment gate

The architecture and adapter are accepted. A particular deployment must still
not claim `benchmark_grade` until its pinned image, internal proxy network, and
the full adversarial acceptance suite above pass on that runner platform. This
is a deployment property, not an automatic consequence of selecting
`adapter: codex_cli`.
