# Handoff — notification severity levels

**From:** the host/watchdog side (prana `host/alerts.py`, `deadman.ps1`,
`job-watch.ps1`) · 2026-09-07
**To:** the session building the brain push / notify-suti fan-out / app
Notifications view (`brain/push.py`, recent commits `3088101`,
`0a4a9a4`, `927d629`).
**Why:** notifications need a **severity level** so downstream can treat
them differently — feed-only vs badge vs OS-push vs escalate. The phone
app is the primary consumer. This is a **shared-interface** decision:
you own the brain+app side, I own the producer side; we need to agree
the contract once so we don't build two incompatible halves.

## The problem this solves

Right now every notification is delivered identically — `push.send()`
logs to the feed AND Web Pushes to every device, and the host's
`AlertManager`/`deadman` Telegram-page on everything. There is no way to
say "record this but don't buzz me." Result (real, last night): ~a dozen
pages to Suti's Telegram, and **almost none were things he needed to act
on** — they were recoveries, drill artifacts, and self-healing
restart-storms from deliberate host bounces. A level on each
notification fixes this at the root instead of case-by-case.

## The four levels (Suti's taxonomy)

The **producer sets the level** (it knows the meaning). A **downstream
routing table maps level → channels** (policy in one place; the app can
override per-level later). Keep these two concerns separate.

| Level | Meaning | Proposed routing |
|---|---|---|
| `log` | happened, no action needed | feed only |
| `warn` | degraded but self-healing / not urgent | feed + in-app badge (no OS push) |
| `error` | wrong, needs attention, not catastrophic | feed + Web Push + Telegram |
| `emergency` | can't self-recover / the safety net is down | feed + push + Telegram + escalate (repeat; later SMS/call) |

**Mapping, so the boundaries are concrete** (from real events):
- `log` — "akhada-brain recovered", "deadman restarted the host OK",
  drill pages, boot-time component churn that self-heals.
- `warn` — "restart storm → supervisor cooldown" that then recovers; a
  single overdue or failed scheduled run.
- `error` — a scheduled job failing *repeatedly*; a component unhealthy
  past its episode deadline and *staying* down; `smriti-nightly` failed.
- `emergency` — deadman **restart budget EXHAUSTED** ("host not coming
  back on its own"); "Task Scheduler / deadman not firing".

## The contract (what the app codes against)

Extend the current feed entry (`notifications.jsonl`, today
`{at, title, body}`) to:

```json
{ "at": "...", "level": "log|warn|error|emergency",
  "source": "host/akhada-brain" | "deadman" | "job-watch/smriti-nightly" | ...,
  "title": "...", "body": "...",
  "category": "<dedup key>", "url": "./#/?notifications=1" }
```

- `source` — who emitted it (lets the app group/filter).
- `category` — a stable dedup key (e.g. `akhada-brain/restart-storm`) so
  repeats **collapse in the feed with a count** instead of spamming.
- `level` + `url` as above.

## Division of labour

**Your side (brain + app) — the feed, the push, the rendering:**
1. `push.py`: add `level`, `source`, `category` to `send()` and
   `_log_notification()`; persist them in `notifications.jsonl`;
   `read_notifications()` returns them.
2. **Routing lives here:** `send()` (or a small router it calls) decides
   per-level whether to actually Web Push. The feed ALWAYS records every
   level; only `error`/`emergency` Web Push. `log`/`warn` are feed-only.
3. App Notifications view: render by level (icon/colour), a filter, a
   badge for unread `warn`+, OS push only for `error`/`emergency`. Deep
   link unchanged.
4. Expose a way for the host to emit a level-tagged notification —
   either `push.send(..., level=...)` callable in-process, or a small
   authenticated brain endpoint (`POST /v1/narada/notify` with the
   contract) the host can call over 8811.

**My side (host + watchdog) — the producers:**
5. `AlertManager` emits level-tagged notifications through your path:
   `recovered` → `log`, `restart-storm/cooldown` → `warn`,
   `unhealthy-past-deadline-still-down` → `error`. (This alone kills the
   every-bounce Telegram noise.)
6. `deadman.ps1` / `job-watch.ps1` adopt the same levels: successful
   restart → `log`, overdue/failed job → `error`, budget-exhausted /
   scheduler-not-firing → `emergency`.

## The one constraint you need from my side

**The deadman must keep an INDEPENDENT delivery path.** It is the
watchdog that pages when the brain/host is *down* — so it cannot route
through the brain's feed for its `error`/`emergency` pages (the brain
may be the dead thing). Plan: the deadman self-delivers `error`+
`emergency` via its own Telegram (as today), and writes ALL levels to a
local file that the host reconciles into the brain feed when it's up.
So: **most notifications flow through your brain feed; the watchdog's
own pages are the deliberate exception.** Don't design the feed assuming
it's the only producer.

## Decisions I need from you (push back freely)

1. **Default level** for callers that don't set one. I propose `error`
   (preserves today's push-everything behaviour during migration; we
   then tag callers down to their real level). Agree?
2. **Where the routing table lives** — inside `push.send()`, or a
   separate `route(notification)` the producers and `send()` both call?
3. **Telegram's future role** — secondary sink for `error`+`emergency`
   only, or retired once the app push is trusted? (Host `AlertManager`
   Telegrams today; I'd route it through your feed and let the feed fan
   out to Telegram for `error`+, rather than AlertManager Telegramming
   directly. OK?)
4. **Dedup/collapse** in the feed by `category` — you own the store, so
   your call on whether the feed collapses repeats or the app does.
5. **Emergency escalation** — is repeat-until-ack / SMS in scope now, or
   just "push + Telegram, louder" for v1?

## Suggested next step

Once we agree 1–5, this becomes a short spec and gets a cross-review
(the house pattern for a cross-cutting contract), then we each build our
half against the agreed contract. Happy to write that spec from your
answers, or fold my producer-side changes into whatever you're already
writing — your call on who holds the pen for the brain-side contract.
