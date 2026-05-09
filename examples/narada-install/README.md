# Narada install files

The heartbeat daemon reads all runtime config from the memory tree at
`~/.narada/`. None of it is hardcoded. This directory is the reference —
copy it into the memory tree to bootstrap a new install.

## First-time install

```bash
mkdir -p ~/.narada/heartbeat
cp -r examples/narada-install/heartbeat/* ~/.narada/heartbeat/
touch ~/.narada/tasks.md   # empty task list to start
```

Then edit `~/.narada/heartbeat/wake.md` to taste — it's the single source
of truth for what the daemon reads, where cycle logs go, and what state
variables are available.

## What's here

- `heartbeat/wake.md` — the manifest. Declares prompts, state sources, cycle log path.
- `heartbeat/system-prompt.md` — viveka's identity prompt.
- `heartbeat/desire-prompt.md` — desire generation template (uses state placeholders).
- `heartbeat/judgment-prompt.md` — plan judgment template.
- `heartbeat/executor-boundaries.md` — sandbox rules for the delegate (claude -p).

## Why no Python fallback

The daemon will not start if these files are missing. There used to be
hardcoded fallback strings in `viveka.py` and `delegate.py`; they were
removed 2026-04-16 because maintaining two copies of each prompt is a
drift-prone mess. If you want different behaviour, edit the memory-tree
file. That file is the authority.

## Tree layout after install

```
~/.narada/
├── identity.md              (provided separately — your identity spine)
├── mind.md
├── suti.md
├── manifest.md
├── MEMORY.md
├── tasks.md
├── heartbeat/
│   ├── wake.md              ← manifest (this install copies it here)
│   ├── system-prompt.md
│   ├── desire-prompt.md
│   ├── judgment-prompt.md
│   ├── executor-boundaries.md
│   ├── artifacts/           (executor output per cycle)
│   └── cycles/              (daemon's cycle logs, auto-created)
└── .smriti/                 (smriti index + queue, auto-created)
```
