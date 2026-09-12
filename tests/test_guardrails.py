"""Unit tests for risk guardrails, whitelist checks, and account ring-fencing."""

import pytest
from src.backend.config import settings
from src.backend.guardrails import GuardrailViolation, risk_guard


@pytest.fixture(autouse=True)
def reset_state():
    risk_guard.reset()
    settings.max_order_usd = 100.0
    settings.daily_trade_limit_usd = 500.0
    settings.asset_whitelist = "SPY,QQQ,AAPL,NVDA,TSLA"
    settings.execution_mode = "autonomous"
    yield
    risk_guard.reset()


def test_asset_whitelist_allowed():
    # SPY is on whitelist
    risk_guard.validate_order(symbol="SPY", side="buy", amount_usd=50.0)


def test_asset_whitelist_blocked():
    # GME is not on whitelist
    with pytest.raises(GuardrailViolation) as excinfo:
        risk_guard.validate_order(symbol="GME", side="buy", amount_usd=50.0)
    assert "Asset Whitelist Violation" in str(excinfo.value)


def test_max_order_cap_enforced():
    with pytest.raises(GuardrailViolation) as excinfo:
        risk_guard.validate_order(symbol="NVDA", side="buy", amount_usd=150.0)
    assert "Order Limit Violation" in str(excinfo.value)


def test_cumulative_daily_budget_enforced():
    settings.daily_trade_limit_usd = 100.0
    risk_guard.record_executed_trade({"symbol": "SPY", "side": "buy", "amount_usd": 80.0})

    with pytest.raises(GuardrailViolation) as excinfo:
        risk_guard.validate_order(symbol="QQQ", side="buy", amount_usd=30.0)
    assert "Daily Budget Violation" in str(excinfo.value)


def test_ring_fencing_blocks_retirement_accounts():
    with pytest.raises(GuardrailViolation) as excinfo:
        risk_guard.validate_order(
            symbol="SPY",
            side="buy",
            amount_usd=50.0,
            account_type="Roth IRA",
        )
    assert "Ring-Fencing Violation" in str(excinfo.value)


def test_sell_order_does_not_consume_daily_budget():
    risk_guard.record_executed_trade({"symbol": "SPY", "side": "sell", "amount_usd": 100.0})
    status = risk_guard.get_status()
    assert status["today_cumulative_spend_usd"] == 0.0


def test_market_closed_policy_blocks_live_trading():
    from unittest.mock import patch
    with patch("src.backend.guardrails.is_market_open", return_value=False):
        # Paper order works even if market is closed
        risk_guard.validate_order(symbol="SPY", side="buy", amount_usd=50.0, is_live=False)

        # Live order raises GuardrailViolation
        with pytest.raises(GuardrailViolation) as excinfo:
            risk_guard.validate_order(symbol="SPY", side="buy", amount_usd=50.0, is_live=True)
        assert "Market Closed Policy" in str(excinfo.value)


def test_market_open_allows_live_trading():
    from unittest.mock import patch
    with patch("src.backend.guardrails.is_market_open", return_value=True):
        risk_guard.validate_order(symbol="SPY", side="buy", amount_usd=50.0, is_live=True)


def test_market_hours_sessions():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.backend.market_hours import EASTERN_TZ, get_market_status, is_market_open

    # Wednesday 11:30 AM ET -> Market Open
    wed_open = datetime(2026, 9, 16, 11, 30, 0, tzinfo=EASTERN_TZ)
    assert is_market_open(wed_open) is True
    status = get_market_status(wed_open)
    assert status["is_open"] is True
    assert status["session"] == "regular_hours"

    # Wednesday 8:15 AM ET -> Pre-market
    wed_pre = datetime(2026, 9, 16, 8, 15, 0, tzinfo=EASTERN_TZ)
    assert is_market_open(wed_pre) is False
    assert get_market_status(wed_pre)["session"] == "pre_market"

    # Wednesday 6:00 PM ET -> After-hours
    wed_post = datetime(2026, 9, 16, 18, 0, 0, tzinfo=EASTERN_TZ)
    assert is_market_open(wed_post) is False
    assert get_market_status(wed_post)["session"] == "after_hours"

    # Saturday 2:00 PM ET -> Weekend
    sat = datetime(2026, 9, 19, 14, 0, 0, tzinfo=EASTERN_TZ)
    assert is_market_open(sat) is False
    assert get_market_status(sat)["session"] == "weekend"

