"""Import probes that never instantiate models or open a network."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModuleProbe:
    name: str
    available: bool


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def probe_modules(*names: str) -> tuple[ModuleProbe, ...]:
    return tuple(ModuleProbe(name=name, available=module_available(name)) for name in names)
