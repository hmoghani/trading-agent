"""Risk guardrails and ring-fencing for autonomous and supervised agent trading."""

from datetime import date
import logging
import threading
from typing import Any, Dict, List, Optional
from src.backend.config import settings
from src.backend.market_hours import is_market_open, get_market_status

logger = logging.getLogger(__name__)


class GuardrailViolation(Exception):
    """Raised when an order breaches risk policies or account boundaries."""
    pass


class RiskGuard:
    """Enforces safety bounds, ring-fencing, circuit breakers, and paper trading simulation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_date: date = date.today()
        self._daily_spent_usd: float = 0.0
        self._trade_history: List[Dict[str, Any]] = []
        self._circuit_breaker_active: bool = False
        self._max_drawdown_percent: float = 3.0  # 3.0% daily max loss
        self._peak_prices: Dict[str, float] = {}

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._current_date:
            self._current_date = today
            self._daily_spent_usd = 0.0
            self._circuit_breaker_active = False

    def check_portfolio_drawdown(self, day_return_percent: float) -> None:
        """Trip circuit breaker if portfolio daily loss exceeds drawdown threshold."""
        with self._lock:
            if day_return_percent <= -abs(self._max_drawdown_percent):
                if not self._circuit_breaker_active:
                    self._circuit_breaker_active = True
                    logger.critical(
                        f"🚨 CIRCUIT BREAKER TRIPPED: Daily portfolio drawdown reached {day_return_percent:.2f}% "
                        f"(Threshold: -{self._max_drawdown_percent:.1f}%). Halting all buy executions."
                    )

    def reset_circuit_breaker(self) -> None:
        """Manually reset and unfreeze the circuit breaker."""
        with self._lock:
            self._circuit_breaker_active = False
            logger.info("Circuit breaker manually reset by operator.")

    def update_peak_price(self, symbol: str, current_price: float) -> None:
        """Track high water mark price for trailing stop loss."""
        with self._lock:
            prev_peak = self._peak_prices.get(symbol, current_price)
            self._peak_prices[symbol] = max(prev_peak, current_price)

    def should_trigger_trailing_stop(self, symbol: str, current_price: float, trail_percent: float = 2.5) -> bool:
        """Return True if price dropped more than trail_percent below peak price."""
        with self._lock:
            peak = self._peak_prices.get(symbol, current_price)
            drop = ((peak - current_price) / peak) * 100
            return drop >= trail_percent

    def get_status(self) -> Dict[str, Any]:
        """Return current risk metrics and configuration status."""
        with self._lock:
            self._reset_if_new_day()
            return {
                "execution_mode": settings.execution_mode,
                "dry_run": settings.dry_run,
                "max_order_usd": settings.max_order_usd,
                "daily_trade_limit_usd": settings.daily_trade_limit_usd,
                "today_cumulative_spend_usd": round(self._daily_spent_usd, 2),
                "remaining_daily_budget_usd": round(
                    max(0.0, settings.daily_trade_limit_usd - self._daily_spent_usd), 2
                ),
                "whitelisted_symbols": settings.whitelisted_symbols,
                "strategy_interval_seconds": settings.strategy_interval_seconds,
                "circuit_breaker_active": self._circuit_breaker_active,
                "max_drawdown_percent": self._max_drawdown_percent,
            }

    def validate_order(
        self,
        symbol: str,
        side: str,
        amount_usd: float,
        account_type: Optional[str] = None,
        is_live: bool = False,
    ) -> None:
        """Validate order against whitelist, dollar caps, account restrictions, and regular market hours."""
        symbol = symbol.strip().upper()
        side = side.strip().lower()

        # Regular Trading Hours (RTH) Policy for Live Orders
        if is_live and not is_market_open():
            status = get_market_status()
            raise GuardrailViolation(
                f"Market Closed Policy: Live trading is restricted strictly to Regular Trading Hours (Monday–Friday, 9:30 AM – 4:00 PM ET). "
                f"Current status: {status['status_text']} ({status['current_time_et']}). "
                f"Next regular session opens {status['next_open_et']}."
            )

        # 0. Circuit Breaker Check
        if self._circuit_breaker_active and side == "buy":
            raise GuardrailViolation(
                "🚨 Circuit Breaker Active: Maximum daily portfolio drawdown threshold breached. "
                "All automated buy executions are locked until manually reset or next trading day."
            )

        # 1. Ring-Fencing: Account isolation
        if account_type:
            account_type_clean = account_type.lower()
            if any(forbidden in account_type_clean for forbidden in ["ira", "retirement", "joint", "custodial"]):
                raise GuardrailViolation(
                    f"Ring-Fencing Violation: Trading in '{account_type}' accounts is blocked. "
                    f"The agent is restricted to standard individual brokerage accounts."
                )

        # 2. Asset Whitelist Check
        if symbol not in settings.whitelisted_symbols:
            raise GuardrailViolation(
                f"Asset Whitelist Violation: Symbol '{symbol}' is not in the approved whitelist "
                f"({', '.join(settings.whitelisted_symbols)})."
            )

        # 3. Single Order Ceiling Check
        if amount_usd > settings.max_order_usd:
            raise GuardrailViolation(
                f"Order Limit Violation: Estimated order value ${amount_usd:.2f} for {symbol} "
                f"exceeds maximum allowed order cap of ${settings.max_order_usd:.2f}."
            )

        # 4. Daily Cumulative Spend Check (Purchases only)
        if side == "buy":
            with self._lock:
                self._reset_if_new_day()
                if self._daily_spent_usd + amount_usd > settings.daily_trade_limit_usd:
                    remaining = max(0.0, settings.daily_trade_limit_usd - self._daily_spent_usd)
                    raise GuardrailViolation(
                        f"Daily Budget Violation: Buying ${amount_usd:.2f} of {symbol} would exceed "
                        f"remaining daily allowance of ${remaining:.2f} (Daily limit: ${settings.daily_trade_limit_usd:.2f})."
                    )

    def record_executed_trade(self, trade_record: Dict[str, Any]) -> None:
        """Record trade details into local ledger and update daily spending."""
        side = trade_record.get("side", "").lower()
        amount_usd = float(trade_record.get("amount_usd", 0.0))

        with self._lock:
            self._reset_if_new_day()
            if side == "buy":
                self._daily_spent_usd += amount_usd
            self._trade_history.insert(0, trade_record)
            if len(self._trade_history) > 100:
                self._trade_history.pop()

    def get_trade_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent trade history."""
        with self._lock:
            return list(self._trade_history[:limit])

    def reset(self) -> None:
        """Reset internal metrics (for testing)."""
        with self._lock:
            self._current_date = date.today()
            self._daily_spent_usd = 0.0
            self._trade_history.clear()


risk_guard = RiskGuard()
