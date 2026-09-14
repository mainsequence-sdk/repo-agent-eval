from __future__ import annotations

import subprocess
from pathlib import Path

from ms_agent_eval.core.models import GitSource, SourceRef, SourceRefKind
from ms_agent_eval.core.sources import GitHubSourceProvider


def test_private_github_resolution_uses_credential_helper_without_command_secret(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []
    commit = "a" * 40

    def fake_run(
        arguments: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, kwargs
        commands.append(arguments)
        environments.append(env)
        if arguments[:3] == ["git", "credential", "fill"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout="protocol=https\nhost=github.com\nusername=user\npassword=secret-token\n",
                stderr="",
            )
        if "GIT_CONFIG_COUNT" not in env:
            raise subprocess.CalledProcessError(
                128,
                arguments,
                stderr="authentication required",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"{commit}\trefs/tags/v1.0.0\n",
            stderr="",
        )

    monkeypatch.setattr("ms_agent_eval.core.sources.subprocess.run", fake_run)
    provider = GitHubSourceProvider()
    source = GitSource(
        "https://github.com/example/private-repository",
        SourceRef(SourceRefKind.TAG, "v1.0.0"),
    )

    resolved = provider.resolve(source)

    assert resolved.resolved_commit == commit
    assert any(command[:3] == ["git", "credential", "fill"] for command in commands)
    assert all("secret-token" not in " ".join(command) for command in commands)
    authenticated = [
        environment for environment in environments if environment.get("GIT_CONFIG_COUNT") == "1"
    ]
    assert len(authenticated) == 1
    assert authenticated[0]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert authenticated[0]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
