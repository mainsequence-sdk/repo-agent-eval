from __future__ import annotations

import os
import re
import subprocess
from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .errors import ResolutionError
from .models import GitSource, SourceRef, SourceRefKind


_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ResolvedSource:
    repository_url_requested: str
    repository_url_canonical: str
    requested_ref: SourceRef
    resolved_commit: str


class SourceProvider(Protocol):
    def resolve(self, source: GitSource) -> ResolvedSource: ...

    def materialize(self, source: ResolvedSource, destination: Path) -> None: ...


GitRunner = Callable[[Sequence[str], Path | None], str]


def run_git(
    arguments: Sequence[str],
    cwd: Path | None = None,
    *,
    extra_environment: dict[str, str] | None = None,
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    if extra_environment:
        environment.update(extra_environment)
    try:
        completed = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        stderr = getattr(error, "stderr", "") or ""
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else type(error).__name__
        raise ResolutionError(f"Git command failed safely: {detail}") from error
    return completed.stdout


@dataclass(frozen=True)
class GitCredential:
    protocol: str
    host: str
    username: str
    password: str


def _credential_from_helper(repository_url: str) -> GitCredential | None:
    """Read a configured credential without putting its value in process arguments."""

    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return None
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    request = f"protocol=https\nhost={parsed.hostname}\n\n"
    try:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            input=request,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values = dict(
        line.split("=", maxsplit=1)
        for line in completed.stdout.splitlines()
        if "=" in line
    )
    username = values.get("username", "")
    password = values.get("password", "")
    if not username or not password:
        return None
    return GitCredential(parsed.scheme, parsed.hostname, username, password)


def _credential_environment(credential: GitCredential) -> dict[str, str]:
    encoded = b64encode(
        f"{credential.username}:{credential.password}".encode("utf-8")
    ).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": (
            f"http.{credential.protocol}://{credential.host}/.extraHeader"
        ),
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
    }


class GitHubSourceProvider:
    def __init__(self, runner: GitRunner = run_git) -> None:
        self._runner = runner
        self._credential: GitCredential | None = None
        self._credential_attempted = False

    def _run(self, arguments: Sequence[str], cwd: Path | None = None) -> str:
        if self._credential is not None:
            return run_git(
                arguments,
                cwd,
                extra_environment=_credential_environment(self._credential),
            )
        try:
            return self._runner(arguments, cwd)
        except ResolutionError:
            if self._runner is not run_git or self._credential_attempted:
                raise
            self._credential_attempted = True
            repository_url = next(
                (
                    item
                    for item in arguments
                    if isinstance(item, str) and item.startswith("https://github.com/")
                ),
                "https://github.com",
            )
            credential = _credential_from_helper(repository_url)
            if credential is None:
                raise
            self._credential = credential
            return run_git(
                arguments,
                cwd,
                extra_environment=_credential_environment(credential),
            )

    @staticmethod
    def canonical_url(repository_url: str) -> str:
        return repository_url.removesuffix("/").removesuffix(".git") + ".git"

    def tags(self, source: GitSource) -> tuple[str, ...]:
        canonical = self.canonical_url(source.repository_url)
        output = self._run(["ls-remote", "--tags", canonical], None)
        tags: set[str] = set()
        for line in output.splitlines():
            fields = line.split("\t", maxsplit=1)
            if len(fields) != 2 or not fields[1].startswith("refs/tags/"):
                continue
            name = fields[1].removeprefix("refs/tags/").removesuffix("^{}")
            if name:
                tags.add(name)
        return tuple(sorted(tags, reverse=True))

    def resolve(self, source: GitSource) -> ResolvedSource:
        canonical = self.canonical_url(source.repository_url)
        if source.submodules or source.git_lfs:
            raise ResolutionError(
                "submodules and Git LFS are disabled by the initial safe provider"
            )
        if source.ref.type is SourceRefKind.COMMIT:
            commit = source.ref.value
        else:
            ref = f"refs/tags/{source.ref.value}"
            output = self._run(["ls-remote", "--tags", canonical, ref, f"{ref}^{{}}"], None)
            matches: dict[str, str] = {}
            for line in output.splitlines():
                fields = line.split("\t", maxsplit=1)
                if len(fields) == 2 and _COMMIT_PATTERN.fullmatch(fields[0]):
                    matches[fields[1]] = fields[0]
            commit = matches.get(f"{ref}^{{}}") or matches.get(ref)
            if commit is None:
                raise ResolutionError(f"GitHub tag {source.ref.value!r} was not found")
        if not _COMMIT_PATTERN.fullmatch(commit):
            raise ResolutionError("source did not resolve to a full lowercase commit SHA")
        return ResolvedSource(source.repository_url, canonical, source.ref, commit)

    def materialize(self, source: ResolvedSource, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        self._run(["init", "--quiet"], destination)
        self._run(["remote", "add", "origin", source.repository_url_canonical], destination)
        self._run(
            ["fetch", "--quiet", "--depth=1", "--no-tags", "origin", source.resolved_commit],
            destination,
        )
        self._run(["checkout", "--quiet", "--detach", "FETCH_HEAD"], destination)
        actual = self._run(["rev-parse", "HEAD"], destination).strip()
        if actual != source.resolved_commit:
            raise ResolutionError(
                f"materialized commit {actual!r} differs from lock {source.resolved_commit!r}"
            )
