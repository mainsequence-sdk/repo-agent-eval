from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import questionary


@dataclass(frozen=True)
class Choice:
    label: str
    value: str


class PromptBackend(Protocol):
    def text(self, message: str, *, default: str | None = None) -> str: ...

    def secret(self, message: str) -> str: ...

    def select(self, message: str, choices: Sequence[Choice]) -> str: ...

    def checkbox(self, message: str, choices: Sequence[Choice]) -> tuple[str, ...]: ...

    def confirm(self, message: str, *, default: bool = False) -> bool: ...

    def write(self, message: str) -> None: ...


def _required(value: object) -> object:
    if value is None:
        raise KeyboardInterrupt
    return value


class QuestionaryPromptBackend:
    def text(self, message: str, *, default: str | None = None) -> str:
        value = _required(questionary.text(message, default=default or "").ask())
        return str(value).strip()

    def secret(self, message: str) -> str:
        return str(_required(questionary.password(message).ask())).strip()

    def select(self, message: str, choices: Sequence[Choice]) -> str:
        rendered = [questionary.Choice(item.label, value=item.value) for item in choices]
        return str(_required(questionary.select(message, choices=rendered).ask()))

    def checkbox(self, message: str, choices: Sequence[Choice]) -> tuple[str, ...]:
        rendered = [questionary.Choice(item.label, value=item.value) for item in choices]
        value = _required(questionary.checkbox(message, choices=rendered).ask())
        assert isinstance(value, list)
        return tuple(str(item) for item in value)

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return bool(_required(questionary.confirm(message, default=default).ask()))

    def write(self, message: str) -> None:
        print(message)
