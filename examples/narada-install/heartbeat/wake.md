---
# Heartbeat wake manifest.
#
# Loaded once per cycle by the daemon. Everything about what Narada reads,
# where cycle logs go, and what state variables are available is declared
# here. No defaults live in Python — edit this file to change behavior.

# Prompt files loaded fresh each cycle. Paths are relative to the memory
# tree root (~/.narada/).
prompts:
  system: heartbeat/system-prompt.md
  desire: heartbeat/desire-prompt.md
  judgment: heartbeat/judgment-prompt.md
  executor: heartbeat/executor-boundaries.md

# State variables substituted into {placeholders} in desire-prompt.md.
# Each entry declares how to resolve the variable. Supported `source`
# kinds: clock, latest_cycle, recent_cycles, file_section, shell.
state:
  time:
    source: clock
    format: "%Y-%m-%d %H:%M UTC"

  last_heartbeat:
    source: latest_cycle
    field: started
    fallback: never

  recent_events:
    source: recent_cycles
    count: 5
    line_format: "- {started}: {action} — {topic}"
    fallback: "no recent activity"

  pending_tasks:
    source: file_section
    path: tasks.md
    section: Active
    fallback: "no pending tasks"

  system_health:
    source: shell
    command: ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,memory.used", "--format=csv,noheader,nounits"]
    timeout: 5
    format: "GPU: {0}°C, {1}W, {2}MB VRAM"
    fallback: "system health unknown"

# Where each cycle's log gets written. Variables: {yyyy_mm_dd}, {yyyy_mm},
# {hhmm}, {yyyy_mm_dd_hhmm}, {action}, {topic_slug}.
cycle_log:
  dir_pattern: "heartbeat/cycles/{yyyy_mm}"
  file_pattern: "{yyyy_mm_dd_hhmm}-{action}.md"

# Files whose content is appended to the executor's system prompt on each
# cycle. Gives the executor persistent identity context. Leave empty to
# rely purely on the LoRA weights + wake prompts.
wake_reads: []
---

# Narada — Wake Manifest

This file is read at the start of every heartbeat cycle. It declares:

1. **Which prompts** the viveka and executor use (`prompts:`)
2. **How state is assembled** for the desire prompt (`state:`)
3. **Where cycle logs are written** (`cycle_log:`)
4. **What identity files** the executor always sees (`wake_reads:`)

## Editing this file

Edit the frontmatter above. The daemon re-reads it each cycle, so
changes take effect on the next wake — no daemon restart needed.

If the YAML fails to parse, the cycle aborts with a clear error
pointing at this file. The previous wake is **not** used as fallback —
bad config must be fixed, not silently worked around.

## State variable kinds

| source | what it does |
|---|---|
| `clock` | current UTC time formatted per `format:` |
| `latest_cycle` | frontmatter field from the most recent cycle log |
| `recent_cycles` | last N cycle logs, each rendered via `line_format:` |
| `file_section` | markdown `## section` body extracted from `path:` |
| `shell` | run `command:`, optionally format stdout via `format:` |

All kinds accept a `fallback:` value used when resolution fails.

## Cycle log path variables

- `{yyyy_mm_dd}` → `2026-04-16`
- `{yyyy_mm}` → `2026-04`
- `{hhmm}` → `0024`
- `{yyyy_mm_dd_hhmm}` → `2026-04-16-0024`
- `{action}` → `RESEARCH` (from the viveka's desire)
- `{topic_slug}` → slugified desire topic (max 40 chars)

## Placeholders in desire-prompt.md

The desire prompt template uses Python `.format()` on the assembled
state. Each key in `state:` above must have a matching `{key}` in
`desire-prompt.md`, or `.format()` will raise KeyError at cycle time.

Standard keys this installation uses: `time`, `last_heartbeat`,
`recent_events`, `pending_tasks`, `system_health`.
