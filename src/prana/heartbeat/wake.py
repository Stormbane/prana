"""Wake manifest loader for the heartbeat daemon.

The wake manifest lives at ``~/.narada/heartbeat/wake.md`` and declares
everything the daemon needs at cycle start: which prompts to load, how to
assemble state for the desire prompt, and where to write cycle logs. It
replaces three layers of prior indirection (hardcoded prompts in Python,
per-file path constants, SQLite state queries) with one YAML frontmatter.

Design principles:

- **Single source of truth.** No Python fallbacks. If the manifest is
  missing or malformed, the daemon aborts with a clear error pointing
  to ``examples/narada-install/``.
- **Re-read every cycle.** Changes to the manifest apply on the next
  wake without a daemon restart.
- **Runtime-resolved state.** State variables are resolved at cycle time
  by dispatching on the ``source`` field.

Manifest schema (frontmatter keys):

    prompts:
      system: <path relative to narada root>
      desire: <path>
      judgment: <path>
      executor: <path>

    state:
      <name>:
        source: clock | latest_cycle | recent_cycles | file_section | shell
        ... (kind-specific fields)

    cycle_log:
      dir_pattern: <path-template>
      file_pattern: <filename-template>

    wake_reads: [<path>, ...]
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


NARADA_ROOT = Path.home() / ".narada"
WAKE_PATH = NARADA_ROOT / "heartbeat" / "wake.md"


class WakeManifestError(RuntimeError):
    """Raised when wake.md is missing, malformed, or references bad paths."""


@dataclass
class WakeManifest:
    """Parsed contents of ``~/.narada/heartbeat/wake.md``."""

    prompts: dict[str, Path]           # name -> absolute path
    state: dict[str, dict]             # name -> source spec
    cycle_log_dir_pattern: str
    cycle_log_file_pattern: str
    wake_reads: list[Path] = field(default_factory=list)
    source_file: Path = WAKE_PATH


# ── Loader ──────────────────────────────────────────────────────────


def load_manifest(path: Path | None = None) -> WakeManifest:
    """Read and parse the wake manifest.

    Raises :class:`WakeManifestError` with an actionable message if the
    file is missing or malformed — the daemon should exit on that error,
    not fall back to defaults.
    """
    path = path or WAKE_PATH

    if not path.exists():
        raise WakeManifestError(
            f"wake manifest not found at {path}\n"
            f"Install it from examples/narada-install/heartbeat/wake.md — "
            f"see examples/narada-install/README.md."
        )

    frontmatter = _extract_frontmatter(path.read_text(encoding="utf-8"))
    if frontmatter is None:
        raise WakeManifestError(
            f"{path}: no YAML frontmatter found. Expected ---\\n...\\n--- at file start."
        )

    try:
        import yaml
    except ImportError as e:
        raise WakeManifestError(
            f"PyYAML is required to parse wake.md. Install with: pip install pyyaml"
        ) from e

    try:
        data = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as e:
        raise WakeManifestError(f"{path}: YAML parse error: {e}") from e

    prompts = _require_dict(data, "prompts", path)
    state = _require_dict(data, "state", path)
    cycle_log = _require_dict(data, "cycle_log", path)
    wake_reads = data.get("wake_reads") or []

    resolved_prompts: dict[str, Path] = {}
    for name, rel in prompts.items():
        p = NARADA_ROOT / rel
        if not p.exists():
            raise WakeManifestError(
                f"{path}: prompt '{name}' points to {rel} which does not exist "
                f"under {NARADA_ROOT}"
            )
        resolved_prompts[name] = p

    # Required prompt names for the current codebase
    for required in ("system", "desire", "judgment", "executor"):
        if required not in resolved_prompts:
            raise WakeManifestError(
                f"{path}: prompts.{required} is required but not present"
            )

    resolved_wake_reads: list[Path] = []
    for rel in wake_reads:
        p = NARADA_ROOT / rel
        if not p.exists():
            logger.warning("wake_reads path does not exist: %s", p)
            continue
        resolved_wake_reads.append(p)

    return WakeManifest(
        prompts=resolved_prompts,
        state=state,
        cycle_log_dir_pattern=cycle_log.get("dir_pattern", "heartbeat/cycles"),
        cycle_log_file_pattern=cycle_log.get("file_pattern", "{yyyy_mm_dd_hhmm}.md"),
        wake_reads=resolved_wake_reads,
        source_file=path,
    )


# ── State resolution ────────────────────────────────────────────────


def resolve_state(manifest: WakeManifest) -> dict[str, str]:
    """Resolve each state variable to its current string value.

    Unknown source kinds or runtime failures use the entry's ``fallback``
    value and log a warning — state must always be a complete dict so
    the desire prompt's ``.format()`` succeeds.
    """
    resolved: dict[str, str] = {}
    for name, spec in manifest.state.items():
        if not isinstance(spec, dict):
            logger.warning("state.%s is not a dict, skipping", name)
            resolved[name] = ""
            continue
        kind = spec.get("source", "")
        fallback = spec.get("fallback", "")
        try:
            resolved[name] = _resolve_one(kind, spec) or fallback
        except Exception as e:
            logger.warning("state.%s resolution failed (%s), using fallback", name, e)
            resolved[name] = str(fallback)
    return resolved


def _resolve_one(kind: str, spec: dict) -> str:
    if kind == "clock":
        fmt = spec.get("format", "%Y-%m-%d %H:%M UTC")
        return _dt.datetime.now(_dt.timezone.utc).strftime(fmt)

    if kind == "latest_cycle":
        field_name = spec.get("field", "started")
        latest = _find_latest_cycle()
        if latest is None:
            return ""
        return str(_get_frontmatter_field(latest, field_name) or "")

    if kind == "recent_cycles":
        count = int(spec.get("count", 5))
        line_format = spec.get("line_format", "- {started}: {action} — {topic}")
        cycles = _find_recent_cycles(count)
        if not cycles:
            return ""
        lines = []
        for c in cycles:
            fm = _get_frontmatter(c) or {}
            try:
                lines.append(line_format.format_map(_safe_map(fm)))
            except Exception as e:
                logger.debug("recent_cycles line_format failed on %s: %s", c, e)
        return "\n".join(lines)

    if kind == "file_section":
        rel = spec.get("path", "")
        section = spec.get("section", "")
        full = NARADA_ROOT / rel
        if not full.exists():
            return ""
        return _extract_section(full.read_text(encoding="utf-8"), section)

    if kind == "shell":
        command = spec.get("command", "")
        timeout = int(spec.get("timeout", 5))
        fmt = spec.get("format")
        if not command:
            return ""
        if isinstance(command, str):
            # Allow string form for simple commands; split on shell whitespace
            import shlex
            argv = shlex.split(command)
        else:
            argv = list(command)
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        if result.returncode != 0:
            return ""
        out = result.stdout.strip()
        if fmt:
            parts = [p.strip() for p in out.split(",")]
            try:
                return fmt.format(*parts)
            except Exception:
                return out
        return out

    logger.warning("unknown state source kind: %s", kind)
    return ""


# ── Cycle log path resolution ──────────────────────────────────────


def cycle_log_path(
    manifest: WakeManifest,
    when: _dt.datetime | None = None,
    *,
    action: str = "",
    topic: str = "",
) -> Path:
    """Resolve the absolute path for a cycle log file."""
    when = when or _dt.datetime.now(_dt.timezone.utc)
    vars_ = {
        "yyyy_mm_dd": when.strftime("%Y-%m-%d"),
        "yyyy_mm": when.strftime("%Y-%m"),
        "hhmm": when.strftime("%H%M"),
        "yyyy_mm_dd_hhmm": when.strftime("%Y-%m-%d-%H%M"),
        "action": action,
        "topic_slug": _slugify(topic)[:40],
    }
    dir_part = manifest.cycle_log_dir_pattern.format_map(_safe_map(vars_))
    file_part = manifest.cycle_log_file_pattern.format_map(_safe_map(vars_))
    return NARADA_ROOT / dir_part / file_part


# ── Cycle discovery helpers ────────────────────────────────────────


def _find_latest_cycle() -> Path | None:
    cycles_root = NARADA_ROOT / "heartbeat" / "cycles"
    if not cycles_root.exists():
        return None
    candidates = sorted(cycles_root.rglob("*.md"))
    return candidates[-1] if candidates else None


def _find_recent_cycles(count: int) -> list[Path]:
    cycles_root = NARADA_ROOT / "heartbeat" / "cycles"
    if not cycles_root.exists():
        return []
    candidates = sorted(cycles_root.rglob("*.md"))
    return candidates[-count:]


# ── Frontmatter helpers ────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _extract_frontmatter(text: str) -> str | None:
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _get_frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = _extract_frontmatter(text)
    if not fm:
        return None
    try:
        import yaml
        return yaml.safe_load(fm) or {}
    except Exception:
        return None


def _get_frontmatter_field(path: Path, field_name: str) -> Any:
    fm = _get_frontmatter(path)
    if not fm:
        return None
    return fm.get(field_name)


def _extract_section(content: str, section: str) -> str:
    """Pull lines under ``## {section}`` up to the next ``##`` heading."""
    pattern = re.compile(
        rf"^##\s+{re.escape(section)}\s*$(.+?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        return ""
    body = match.group(1).strip()
    lines = [line for line in body.splitlines() if line.strip()]
    return "\n".join(lines)


# ── Utilities ──────────────────────────────────────────────────────


def _require_dict(data: dict, key: str, path: Path) -> dict:
    val = data.get(key)
    if not isinstance(val, dict):
        raise WakeManifestError(
            f"{path}: key '{key}' is required and must be a mapping"
        )
    return val


class _safe_map(dict):
    """format_map-friendly dict that returns empty string for missing keys."""
    def __missing__(self, key: str) -> str:
        return ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug or "untitled"
