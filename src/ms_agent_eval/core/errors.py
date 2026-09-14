from __future__ import annotations

from pathlib import Path


class AgentEvalError(RuntimeError):
    """Base error for framework-owned failures."""


class ConfigurationError(AgentEvalError):
    def __init__(self, message: str, *, path: Path | None = None) -> None:
        self.path = path
        prefix = f"{path}: " if path is not None else ""
        super().__init__(prefix + message)


class IntegrityError(AgentEvalError):
    pass


class ResolutionError(AgentEvalError):
    pass


class PreflightError(AgentEvalError):
    pass


class RecordingError(AgentEvalError):
    """An external recorder projection or delivery operation failed."""
