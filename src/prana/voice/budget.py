"""Cost guards for the realtime voice loop — fail-closed, race-safe.

Realtime API usage is API-key spend with no subscription ceiling. The
ledger guards it mechanically:

- admission RESERVES the session's maximum charge under a cross-process
  lock — N concurrent rooms cannot collectively pass a cap that only
  fits one of them
- settlement replaces the reservation with actual minutes, split across
  local-day boundaries for sessions crossing midnight
- a corrupt or unreadable ledger REFUSES sessions (fail closed) — spend
  guards that reset on damage are not guards
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_FILE = Path.home() / ".narada" / "voice-budget.json"

DEFAULT_SESSION_CAP_S = 10 * 60      # one conversation: 10 minutes
DEFAULT_DAILY_CAP_MIN = 120          # ~$2/day on 2.1-mini at plan pricing

_LOCK_TIMEOUT_S = 10.0
_RESERVATION_TTL_S = 3600.0          # crashed sessions release after an hour


class BudgetExceeded(RuntimeError):
    pass


class BudgetUnavailable(RuntimeError):
    """Ledger unreadable — refuse to spend (fail closed)."""


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

    # ── locked, atomic ledger IO ─────────────────────────────────────

    def _lock_path(self) -> Path:
        return self.ledger_path.with_suffix(".lock")

    def _acquire_lock(self):
        """O_EXCL lockfile with stale-lock recovery.

        Holders keep the lock for milliseconds; a lock older than the
        timeout belongs to a crashed process and is stolen (same policy
        as the sessions token lock) — a crash must never permanently
        disable voice admission.
        """
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        lock = self._lock_path()
        while True:
            try:
                return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    logger.warning("stealing stale budget lock %s", lock)
                    try:
                        lock.unlink()
                    except OSError:
                        pass
                    deadline = time.monotonic() + _LOCK_TIMEOUT_S
                time.sleep(0.05)

    def _release_lock(self, fd) -> None:
        os.close(fd)
        try:
            self._lock_path().unlink()
        except OSError:
            pass

    def _load(self) -> dict:
        """Read the ledger; corruption is an ERROR, not a reset."""
        if not self.ledger_path.exists():
            return {"days": {}, "reservations": {}}
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BudgetUnavailable(
                f"voice budget ledger unreadable ({exc}) — refusing to "
                f"spend; inspect/repair {self.ledger_path}"
            ) from exc
        if not isinstance(data, dict) or "days" not in data:
            raise BudgetUnavailable(
                f"voice budget ledger malformed — refusing to spend; "
                f"inspect/repair {self.ledger_path}"
            )
        return data

    def _save(self, data: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.ledger_path.parent), prefix=self.ledger_path.name,
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self.ledger_path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _prune(data: dict) -> None:
        days = data["days"]
        if len(days) > 60:
            for old in sorted(days)[:-60]:
                del days[old]
        now = time.time()
        stale = [
            rid for rid, r in data.get("reservations", {}).items()
            if now - float(r.get("epoch", 0)) > _RESERVATION_TTL_S
        ]
        for rid in stale:
            logger.warning("releasing stale budget reservation %s", rid)
            del data["reservations"][rid]

    # ── public surface ───────────────────────────────────────────────

    def spent_today_min(self) -> float:
        data = self._load()
        return float(data["days"].get(date.today().isoformat(), 0.0))

    def _committed_min(self, data: dict, day: str) -> float:
        spent = float(data["days"].get(day, 0.0))
        reserved = sum(
            float(r.get("max_min", 0.0))
            for r in data.get("reservations", {}).values()
        )
        return spent + reserved

    def reserve(self) -> str:
        """Admit a session: reserve its maximum charge or raise.

        Raises BudgetExceeded when spent+reserved would pass the daily
        cap, BudgetUnavailable when the ledger can't be trusted.
        """
        fd = self._acquire_lock()
        try:
            data = self._load()
            self._prune(data)
            day = date.today().isoformat()
            max_min = self.session_cap_s / 60.0
            committed = self._committed_min(data, day)
            if committed + max_min > self.daily_cap_min:
                raise BudgetExceeded(
                    f"daily voice budget: {committed:.0f} min committed "
                    f"+ {max_min:.0f} min requested > cap "
                    f"{self.daily_cap_min:.0f} min"
                )
            rid = uuid.uuid4().hex[:12]
            data.setdefault("reservations", {})[rid] = {
                "start_iso": datetime.now().isoformat(),
                "epoch": time.time(),
                "max_min": max_min,
            }
            self._save(data)
            return rid
        finally:
            self._release_lock(fd)

    def settle(self, reservation_id: str, duration_s: float) -> None:
        """Replace a reservation with actual minutes, split across the
        local-day boundary for sessions that ran over midnight."""
        fd = self._acquire_lock()
        try:
            data = self._load()
            res = data.get("reservations", {}).pop(reservation_id, None)
            try:
                start = datetime.fromisoformat(res["start_iso"]) if res else None
            except (KeyError, ValueError, TypeError):
                start = None
            if start is None:
                start = datetime.now() - timedelta(seconds=duration_s)
            end = start + timedelta(seconds=duration_s)
            days = data["days"]
            cursor = start
            while cursor < end:
                day_end = datetime.combine(
                    cursor.date() + timedelta(days=1), datetime.min.time()
                )
                segment_end = min(end, day_end)
                minutes = (segment_end - cursor).total_seconds() / 60.0
                key = cursor.date().isoformat()
                days[key] = float(days.get(key, 0.0)) + minutes
                cursor = segment_end
            self._prune(data)
            self._save(data)
            spent = float(days.get(date.today().isoformat(), 0.0))
            if spent >= 0.8 * self.daily_cap_min:
                logger.warning(
                    "voice budget at %.0f%% (%.0f/%.0f min today)",
                    100 * spent / self.daily_cap_min, spent,
                    self.daily_cap_min,
                )
        finally:
            self._release_lock(fd)
