"""RegisterConfig: describes one entity as a spreadsheet-style register."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

FieldType = Literal["text", "number", "date", "bool", "enum"]
RegisterAction = Literal["create", "edit", "delete"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    required: bool = False
    max_length: int | None = None
    choices: tuple[str, ...] | None = None
    read_only: bool = False
    compute: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class RegisterConfig:
    name: str
    model: type
    entity_type: str
    fields: tuple[FieldSpec, ...]
    order_by: Any
    require_admin_for: frozenset[RegisterAction] = field(default_factory=frozenset)
