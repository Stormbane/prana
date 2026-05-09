Current state:
- Time: {time}
- Last heartbeat: {last_heartbeat}
- Recent events:
{recent_events}
- Pending tasks:
{pending_tasks}
- System health: {system_health}

Given this state, what do you want to do right now?

Choose ONE action:
- RESEARCH — investigate something you're curious about
- REFLECT — think about something and write to memory
- CHECK_IN — message Suti about something
- CREATE — build or write something
- REST — nothing calls to you right now

Respond with a single JSON object matching this schema:

```json
{{
  "action": "RESEARCH",
  "topic": "what specifically — empty string for REST",
  "reason": "why this matters to you right now, one sentence"
}}
```
