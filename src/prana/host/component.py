"""Component dataclass + YAML loader with ${VAR} substitution.

A component is a supervised subprocess. Components live in
``~/.narada/host/components.yaml`` so the agent framework / chat bridge
/ body brain are swappable without code changes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Substitution variables resolvable inside the YAML. Resolved at load
# time, not at spawn time, so problems show up early.
_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _build_var_table() -> dict[str, str]:
    """Variables available to ${VAR} substitution in components.yaml."""
    home = str(Path.home())
    return {
        "HOME":          home,
        "USERPROFILE":   home,  # Windows alias
        "PRANA_ROOT":    str(Path(__file__).resolve().parent.parent.parent.parent),
        "DEHA_ROOT":     os.environ.get("DEHA_ROOT", str(Path("C:/Projects/deha"))),
        "HERMES_HOME":   os.environ.get("HERMES_HOME", str(Path(home) / ".hermes")),
        "LOCALAPPDATA":  os.environ.get("LOCALAPPDATA", home),
        "NARADA_ROOT":   os.environ.get("NARADA_ROOT", str(Path(home) / ".narada")),
    }


def _substitute(value: Any, table: dict[str, str]) -> Any:
    """Recursively expand ${VAR} in strings inside nested structures."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in table:
                raise ValueError(f"unknown variable ${{{key}}} in components.yaml")
            return table[key]
        return _VAR_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_substitute(v, table) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, table) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class Component:
    """A supervised process. Identity by name; spec is immutable per run."""
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    restart_grace_s: float = 2.0
    enabled: bool = True
    health_url: str | None = None
    health_interval_s: float = 30.0
    wait_for_url: str | None = None  # Phase 3 dependency gate
    description: str = ""

    def spawn_env(self) -> dict[str, str]:
        """Merge component env into os.environ for subprocess spawn."""
        env = dict(os.environ)
        env.update(self.env)
        return env


def load_components(path: Path) -> list[Component]:
    """Parse components.yaml. Raises ValueError on bad shape."""
    if not path.exists():
        raise FileNotFoundError(f"components.yaml not found at {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict) or "components" not in raw:
        raise ValueError(f"{path}: top-level 'components' key required")

    table = _build_var_table()
    substituted = _substitute(raw, table)
    entries = substituted.get("components") or []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'components' must be a list")

    components: list[Component] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: components[{i}] must be a mapping")
        if "name" not in entry:
            raise ValueError(f"{path}: components[{i}] missing 'name'")
        if "command" not in entry:
            raise ValueError(f"{path}: components[{i}] missing 'command'")

        cwd = Path(entry.get("cwd") or Path.cwd())
        components.append(Component(
            name=str(entry["name"]),
            command=list(entry["command"]),
            cwd=cwd,
            env=dict(entry.get("env") or {}),
            restart_grace_s=float(entry.get("restart_grace_s", 2.0)),
            enabled=bool(entry.get("enabled", True)),
            health_url=entry.get("health_url") or None,
            health_interval_s=float(entry.get("health_interval_s", 30.0)),
            wait_for_url=entry.get("wait_for_url") or None,
            description=str(entry.get("description") or ""),
        ))

    # Names must be unique — the supervisor keys by name
    seen = set()
    for c in components:
        if c.name in seen:
            raise ValueError(f"{path}: duplicate component name {c.name!r}")
        seen.add(c.name)

    return components
