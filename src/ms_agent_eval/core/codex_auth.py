from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .errors import ConfigurationError, PreflightError
from .hashing import canonical_json_bytes


class LoginRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: int | None,
    ) -> int: ...


class SubprocessLoginRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: int | None,
    ) -> int:
        try:
            completed = subprocess.run(
                list(arguments),
                env=dict(environment),
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise PreflightError("Codex CLI login could not be started") from error
        return completed.returncode


def default_subscription_auth_root() -> Path:
    return Path.home() / "ms_agent_eval" / "auth" / "codex-subscription"


@dataclass(frozen=True)
class SubscriptionAuthIdentity:
    authentication: str
    codex_version: str


class CodexSubscriptionAuthStore:
    """Own a dedicated ChatGPT subscription login outside experiment workspaces."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_subscription_auth_root()).expanduser().resolve()
        self.auth_file = self.root / "auth.json"
        self.marker_file = self.root / "subscription.json"

    def login(
        self,
        *,
        codex_binary: str = "codex",
        device_auth: bool = True,
        runner: LoginRunner | None = None,
    ) -> SubscriptionAuthIdentity:
        self._prepare_root()
        (self.root / "config.toml").write_text(
            'cli_auth_credentials_store = "file"\n', encoding="utf-8"
        )
        (self.root / "config.toml").chmod(0o600)
        environment = _login_environment(self.root)
        command = [codex_binary, "login"]
        if device_auth:
            command.append("--device-auth")
        exit_code = (runner or SubprocessLoginRunner()).run(
            command,
            environment=environment,
            timeout_seconds=None,
        )
        if exit_code != 0:
            raise PreflightError(f"Codex subscription login failed with exit code {exit_code}")
        self._validate_auth_file()
        version = _codex_version(codex_binary, environment)
        marker = {
            "schema_version": 1,
            "authentication": "chatgpt_subscription",
            "codex_version": version,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.marker_file.write_bytes(canonical_json_bytes(marker))
        self.marker_file.chmod(0o600)
        return SubscriptionAuthIdentity("chatgpt_subscription", version)

    def identity(self) -> SubscriptionAuthIdentity:
        self._validate_root()
        try:
            marker = json.loads(self.marker_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PreflightError(
                "Codex subscription auth marker is missing or invalid; run "
                "`ms-agent-eval codex login`"
            ) from error
        if not isinstance(marker, Mapping):
            raise PreflightError("Codex subscription auth marker must be a JSON object")
        if marker.get("schema_version") != 1 or marker.get("authentication") != (
            "chatgpt_subscription"
        ):
            raise PreflightError(
                "Codex auth was not provisioned as ChatGPT subscription authentication"
            )
        version = marker.get("codex_version")
        if not isinstance(version, str) or not version.strip():
            raise PreflightError("Codex subscription auth marker has no CLI version")
        self._validate_auth_file()
        return SubscriptionAuthIdentity("chatgpt_subscription", version.strip())

    def install_into(self, destination: Path) -> SubscriptionAuthIdentity:
        identity = self.identity()
        destination.mkdir(parents=True, exist_ok=False)
        destination.chmod(0o700)
        target = destination / "auth.json"
        shutil.copyfile(self.auth_file, target, follow_symlinks=False)
        target.chmod(0o600)
        return identity

    def redact(self, payload: bytes) -> bytes:
        """Remove credential values before any process output is persisted."""

        self._validate_auth_file()
        try:
            auth = json.loads(self.auth_file.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PreflightError("Codex subscription auth.json is invalid") from error
        result = payload
        for secret in sorted(_secret_strings(auth), key=len, reverse=True):
            result = result.replace(secret.encode("utf-8"), b"[REDACTED]")
        return result

    def _prepare_root(self) -> None:
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise ConfigurationError("Codex subscription auth root must be a real directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _validate_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise PreflightError(
                "Codex subscription auth is not configured; run `ms-agent-eval codex login`"
            )
        if self.marker_file.is_symlink() or not self.marker_file.is_file():
            raise PreflightError(
                "Codex subscription auth is not configured; run `ms-agent-eval codex login`"
            )
        if self.root.stat().st_mode & 0o077:
            raise PreflightError("Codex subscription auth root must not be group/world accessible")
        if self.marker_file.stat().st_mode & 0o077:
            raise PreflightError("Codex subscription auth marker must not be group/world accessible")

    def _validate_auth_file(self) -> None:
        if self.auth_file.is_symlink() or not self.auth_file.is_file():
            raise PreflightError("Codex subscription auth.json is missing or unsafe")
        mode = self.auth_file.stat().st_mode & 0o777
        if mode & 0o077:
            raise PreflightError("Codex subscription auth.json must not be group/world accessible")
        if self.auth_file.stat().st_size == 0:
            raise PreflightError("Codex subscription auth.json is empty")
        try:
            payload = json.loads(self.auth_file.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PreflightError("Codex subscription auth.json is invalid") from error
        if not isinstance(payload, Mapping):
            raise PreflightError("Codex subscription auth.json must be a JSON object")
        if payload.get("OPENAI_API_KEY"):
            raise PreflightError(
                "Codex auth store contains API-key authentication; run subscription device login"
            )
        if not payload.get("tokens"):
            raise PreflightError("Codex auth store does not contain ChatGPT subscription tokens")


def _login_environment(codex_home: Path) -> dict[str, str]:
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TERM"}
    }
    allowed["CODEX_HOME"] = codex_home.as_posix()
    return allowed


def _codex_version(codex_binary: str, environment: Mapping[str, str]) -> str:
    try:
        completed = subprocess.run(
            [codex_binary, "--version"],
            env=dict(environment),
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PreflightError("Codex CLI version could not be resolved") from error
    value = completed.stdout.strip()
    if not value:
        raise PreflightError("Codex CLI returned an empty version")
    return value


def _secret_strings(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {
            secret
            for item in value.values()
            for secret in _secret_strings(item)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {secret for item in value for secret in _secret_strings(item)}
    if isinstance(value, str) and len(value) >= 8:
        return {value}
    return set()
