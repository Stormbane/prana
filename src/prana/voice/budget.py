"""Cost guards for the realtime voice loop.

Realtime API usage is API-key spend with no subscription ceiling, so
two mechanical guards, both enforced in code:

- per-conversation duration cap (the session closes itself)
- daily conversation-minutes ledger with a hard daily cap; when the cap
  is hit the worker refuses to open new realtime sessions until the
  (local) day rolls over, and says so through the wake chime path.

The ledger deliberately counts *conversation minutes*, not dollars —
minutes are observable locally without trusting usage APIs. Calibrate
the cap from the plan's pricing (~$0.016/min on 2.1-mini).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_FILE = Path.home() / ".narada" / "voice-budget.json"

DEFAULT_SESSION_CAP_S = 10 * 60      # one conversation: 10 minutes
DEFAULT_DAILY_CAP_MIN = 120          # ~$2/day on 2.1-mini at plan pricing


class BudgetExceeded(RuntimeError):
    pass


class VoiceBudget:
    def __init__(
        self,
        ledger_path: Path = LEDGER_FILE,
        *,
        session_cap_s: float = DEFAULT_SESSION_CAP_S,
        daily_cap_min: float = DEFAULT_DAILY_CAP_MIN,
    ) -> None:
        self.ledger_path = ledger_path
        self.session_cap_s = session_cap_s
        self.daily_cap_min = daily_cap_min

    def _load(self) -> dict:
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def spent_today_min(self) -> float:
        return float(self._load().get(date.today().isoformat(), 0.0))

    def check_can_start(self) -> None:
        """Raise BudgetExceeded if today's cap is already spent."""
        spent = self.spent_today_min()
        if spent >= self.daily_cap_min:
            raise BudgetExceeded(
                f"daily voice budget spent ({spent:.0f}/{self.daily_cap_min:.0f} min)"
            )

    def record_session(self, duration_s: float) -> None:
        data = self._load()
        key = date.today().isoformat()
        data[key] = float(data.get(key, 0.0)) + duration_s / 60.0
        # keep the ledger small: only the last 60 days
        if len(data) > 60:
            for old in sorted(data)[:-60]:
                del data[old]
        self._save(data)
        spent = data[key]
        if spent >= 0.8 * self.daily_cap_min:
            logger.warning(
                "voice budget at %.0f%% (%.0f/%.0f min today)",
                100 * spent / self.daily_cap_min, spent, self.daily_cap_min,
            )
