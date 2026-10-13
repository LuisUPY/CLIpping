"""Registros persistentes independientes del proveedor de metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProbeRecord:
    id: str
    source_path: str
    created_at: str
    metadata: dict[str, Any]
