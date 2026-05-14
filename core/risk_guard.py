"""RiskGuard — entry gate.

Stateless except for daily-loss latch state (so the warning email + daily reset are tracked).
Reads live position state from StateManager. Rules:
  1. One position per symbol (absolute).
  2. Max concurrent positions across all symbols.
  3. Max daily loss in USDT — once tripped, blocks new entries for the rest of the UTC day,
     sends a warning email once. Existing positions run their course. Resets next UTC day.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

from utils import general

if TYPE_CHECKING:
    from core.state_manager import StateManager
    from core.strategies.base import Strategy


class RiskGuard:
    def __init__(
        self,
        *,
        state_manager: "StateManager",
        max_concurrent_positions: int,
        max_daily_loss_usdt: float,
    ) -> None:
        self._sm = state_manager
        self._max_concurrent = int(max_concurrent_positions)
        self._max_daily_loss = Decimal(str(max_daily_loss_usdt))
        self._tripped_day: str = ""

    def allow_open(self, symbol: str, strategy: "Strategy") -> bool:
        """Return True if `strategy` may open a position on `symbol` right now."""
        if self._sm.has_position(symbol):
            logger.info("[risk] {} {} blocked: position already open on this symbol.",
                        strategy.tag, symbol)
            return False

        open_count = self._sm.open_position_count()
        if open_count >= self._max_concurrent:
            logger.info("[risk] {} {} blocked: at max concurrent positions ({}/{}).",
                        strategy.tag, symbol, open_count, self._max_concurrent)
            return False

        if self._daily_loss_tripped():
            return False

        return True

    def _daily_loss_tripped(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._tripped_day == today:
            return True

        # Reset on a new UTC day.
        if self._tripped_day and self._tripped_day != today:
            logger.info("[risk] new UTC day {} — daily-loss latch reset.", today)
            self._tripped_day = ""

        pnl = self._sm.daily_pnl()
        if pnl <= -self._max_daily_loss:
            logger.warning("[risk] daily loss limit hit: pnl={} <= -{}. Blocking new entries until next UTC day.",
                           pnl, self._max_daily_loss)
            self._tripped_day = today
            self._send_loss_email(pnl)
            return True
        return False

    def _send_loss_email(self, pnl: Decimal) -> None:
        try:
            general.send_crash_email(
                RuntimeError(f"Daily loss limit reached: pnl={pnl} USDT. "
                             f"New entries blocked until next UTC day.")
            )
        except Exception as exc:
            logger.warning("[risk] could not send daily-loss email: {}", exc)
