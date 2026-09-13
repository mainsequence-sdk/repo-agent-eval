# Codex CLI harness

The `codex_cli` executor runs a model-driven stage in a fresh Docker container.
It uses Codex/ChatGPT subscription authentication. An API exchange may occur as
part of sign-in, but the executor does not accept an OpenAI API key and does not
use API-billed inference.

Codex still sends model requests over HTTPS and the framework records token
counts as an execution budget. Those counts represent subscription usage, not
OpenAI API Platform token charges. API-key authentication cannot be treated as
an authentication-only credential by Codex CLI: selecting it also selects
API-billed inference, so this adapter rejects it.

## 1. Provision dedicated subscription authentication

Install the exact Codex CLI version used by the runner, then authenticate a
dedicated store outside all repositories and experiment data:

```bash
ms-agent-eval codex login
ms-agent-eval codex status
```

By default this store is `~/ms_agent_eval/auth/codex-subscription`. The command
uses device authentication and records only a safe marker beside Codex's secret
`auth.json`. The framework rejects symlinked, empty, or group/world-readable
auth files. Do not commit or copy this directory into an experiment.

## 2. Deploy the isolated control plane

Build a Python 3.12+ runner image containing the exact Codex CLI and required
read-only tools. Push it and resolve its immutable repository digest. Create a
dedicated Docker network with `--internal`, and attach an allowlisting HTTPS
proxy to that network and to its separate egress network:

```text
Codex stage container ── internal network ── allowlisting proxy ── internet
        shell network denied                    OpenAI control plane only
```

The adapter refuses Docker's `bridge`, `default`, `host`, and `none` network
names. Runtime preflight also inspects the selected network and requires
`Internal: true`. The proxy policy is a deployment responsibility and must be
tested against the adversarial suite in ADR-002.

## 3. Configure an executor

```yaml
executors:
  isolated-codex:
    type: harness
    adapter: codex_cli
    version: "0.153.1"
    options:
      image: ghcr.io/your-org/ms-agent-eval-codex@sha256:<64-hex-digest>
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

stages:
  judge:
    executor: isolated-codex
    model: judge
    budget: {model_calls: 3, tokens: 64000, wall_seconds: 600}
```

The image must be digest-pinned, and `version` must match `codex --version`
inside it. `authentication` has exactly one supported value:
`chatgpt_subscription`. There is no API-key fallback.
For a model assigned to this executor, set `reasoning_effort` as a first-class
field beside `name`. Generic endpoint parameters such as `temperature` fail
closed:

```yaml
models:
  judge:
    name: gpt-5.6-sol
    reasoning_effort: high
```

Verify the deployment without making a model request:

```bash
ms-agent-eval codex preflight \
  --executor isolated-codex \
  --workspace workspace.yaml
```

This checks subscription auth, the immutable image, the internal Docker
network, the Docker server version, the exact Codex CLI version, and Python
3.12+ before a stage can consume subscription capacity.

## 4. Execute one locked stage

Start or resume an experiment, find the prepared packet, then execute it:

```bash
ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
ms-agent-eval stage execute <packet-id> --workspace workspace.yaml
```

The coordinator validates the packet hash, creates a fresh stage view, copies
the dedicated subscription auth file into an attempt-local `CODEX_HOME`, checks
the pinned image and internal network, and starts one non-root container. The
container has a read-only root, no host home, no repository mount, no artifact
store mount, bounded CPU/memory/PIDs/output, and only the stage view mounted
read-only. Codex runs with `--ephemeral` and a restrictive permission profile.

For a solver, the view contains only the public task, selected context, and
declared attachments. For a judge, it contains the locked candidate, rubric
criteria, and reference evidence, but not the pass threshold. A calibration
invocation receives only one fixture and never its label or accepted range.

After execution, the coordinator validates the structured output, computes
private pass/fail decisions, stores results and bounded JSONL/stderr artifacts
under the external data root, and deletes the entire temporary attempt tree.
Provenance records the image digest, observed CLI version, stage-view hash,
configuration hash, network policy id, instruction policy, and output artifact
references—never the auth secret.

See [ADR-002](architecture/002-isolated-codex-cli-execution.md) for the threat
model, exact case/view split, failure behavior, and deployment acceptance gate.
