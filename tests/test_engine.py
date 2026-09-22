"""Unit tests for autonomous engine and proposal approval workflow."""

import pytest
from src.backend.config import settings
from src.backend.engine import engine
from src.backend.guardrails import risk_guard


@pytest.fixture(autouse=True)
def reset_engine():
    from src.backend.mcp_client import mcp_client
    engine.stop()
    risk_guard.reset()
    settings.execution_mode = "autonomous"
    settings.dry_run = True
    settings.allow_after_hours = True
    mcp_client.paper.reset()
    yield
    engine.stop()
    risk_guard.reset()
    mcp_client.paper.reset()


@pytest.mark.asyncio
async def test_engine_start_stop():
    assert not engine.is_running
    engine.start()
    assert engine.is_running
    engine.stop()
    assert not engine.is_running


@pytest.mark.asyncio
async def test_supervised_mode_creates_proposal_card():
    settings.execution_mode = "supervised"
    engine.active_strategy_name = "smart_dca"

    await engine.execute_cycle()
    proposals = engine.get_pending_proposals()
    assert len(proposals) == 1
    prop = proposals[0]
    assert prop["side"] == "buy"

    # Approve proposal
    receipt = await engine.approve_proposal(prop["id"])
    assert receipt["status"] == "filled"
    assert len(engine.get_pending_proposals()) == 0


@pytest.mark.asyncio
async def test_supervised_mode_reject_proposal():
    settings.execution_mode = "supervised"
    engine.active_strategy_name = "smart_dca"

    await engine.execute_cycle()
    proposals = engine.get_pending_proposals()
    assert len(proposals) == 1
    prop = proposals[0]

    # Reject proposal
    res = await engine.reject_proposal(prop["id"])
    assert res["status"] == "rejected"
    assert len(engine.get_pending_proposals()) == 0


@pytest.mark.asyncio
async def test_autonomous_mode_executes_directly():
    settings.execution_mode = "autonomous"
    engine.active_strategy_name = "smart_dca"

    await engine.execute_cycle()
    # In autonomous mode, no proposals should linger
    assert len(engine.get_pending_proposals()) == 0
    trades = risk_guard.get_trade_history()
    assert len(trades) >= 1
    assert trades[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_position_take_profit_triggers_autonomous_sell():
    """Verify an open position exceeding take_profit_percent triggers an automated SELL order."""
    from src.backend.mcp_client import mcp_client
    settings.execution_mode = "autonomous"
    settings.take_profit_percent = 3.0

    # Seed an open position in paper account with +4.0% gain
    mcp_client.paper.positions["NVDA"] = {
        "quantity": 1.0,
        "average_buy_price": 100.0,
    }
    # Mock current price to $104.00 (+4.0%)
    async def mock_get_quote(symbol):
        return {"symbol": "NVDA", "last_trade_price": 104.0, "previous_close": 100.0}

    original_get_quote = mcp_client.get_quote
    mcp_client.get_quote = mock_get_quote

    try:
        exits = await engine.evaluate_open_positions()
        assert len(exits) == 1
        exit_receipt = exits[0]
        assert exit_receipt["symbol"] == "NVDA"
        assert exit_receipt["side"] == "sell"
        assert "TAKE PROFIT" in exit_receipt["rationale"]
        # Position should be closed
        assert "NVDA" not in mcp_client.paper.positions
    finally:
        mcp_client.get_quote = original_get_quote
        mcp_client.paper.positions.clear()


@pytest.mark.asyncio
async def test_position_stop_loss_triggers_autonomous_sell():
    """Verify an open position dropping below stop_loss_percent triggers an automated SELL order."""
    from src.backend.mcp_client import mcp_client
    settings.execution_mode = "autonomous"
    settings.stop_loss_percent = 2.0

    # Seed an open position in paper account with -3.0% loss
    mcp_client.paper.positions["AAPL"] = {
        "quantity": 1.0,
        "average_buy_price": 200.0,
    }
    # Mock current price to $194.00 (-3.0%)
    async def mock_get_quote(symbol):
        return {"symbol": "AAPL", "last_trade_price": 194.0, "previous_close": 200.0}

    original_get_quote = mcp_client.get_quote
    mcp_client.get_quote = mock_get_quote

    try:
        exits = await engine.evaluate_open_positions()
        assert len(exits) == 1
        exit_receipt = exits[0]
        assert exit_receipt["symbol"] == "AAPL"
        assert exit_receipt["side"] == "sell"
        assert "STOP LOSS" in exit_receipt["rationale"]
        # Position should be closed
        assert "AAPL" not in mcp_client.paper.positions
    finally:
        mcp_client.get_quote = original_get_quote
        mcp_client.paper.positions.clear()


@pytest.mark.asyncio
async def test_position_trailing_stop_triggers_sell():
    """Verify a position pulling back from a tracked peak triggers trailing stop SELL order."""
    from src.backend.mcp_client import mcp_client
    settings.execution_mode = "autonomous"
    settings.enable_trailing_stop = True
    settings.trailing_stop_percent = 1.5

    # Seed open position
    mcp_client.paper.positions["SPY"] = {
        "quantity": 1.0,
        "average_buy_price": 500.0,
    }
    # Seed peak price at $515.00 (+3.0%)
    risk_guard._peak_prices["SPY"] = 515.0

    # Current price pulled back to $505.00 (pullback of 1.94% from peak $515.00, net gain is still +1.0%)
    async def mock_get_quote(symbol):
        return {"symbol": "SPY", "last_trade_price": 505.0, "previous_close": 500.0}

    original_get_quote = mcp_client.get_quote
    mcp_client.get_quote = mock_get_quote

    try:
        exits = await engine.evaluate_open_positions()
        assert len(exits) == 1
        exit_receipt = exits[0]
        assert exit_receipt["symbol"] == "SPY"
        assert exit_receipt["side"] == "sell"
        assert "TRAILING STOP" in exit_receipt["rationale"]
    finally:
        mcp_client.get_quote = original_get_quote
        mcp_client.paper.positions.clear()
        risk_guard.clear_peak_price("SPY")


@pytest.mark.asyncio
async def test_supervised_mode_creates_sell_proposal():
    """Verify supervised mode generates a proposal card for take-profit exits instead of executing directly."""
    from src.backend.mcp_client import mcp_client
    settings.execution_mode = "supervised"
    settings.take_profit_percent = 2.5

    mcp_client.paper.positions["NVDA"] = {
        "quantity": 1.0,
        "average_buy_price": 100.0,
    }
    async def mock_get_quote(symbol):
        return {"symbol": "NVDA", "last_trade_price": 103.0, "previous_close": 100.0}

    original_get_quote = mcp_client.get_quote
    mcp_client.get_quote = mock_get_quote

    try:
        exits = await engine.evaluate_open_positions()
        # In supervised mode, no direct execution
        assert len(exits) == 0
        proposals = engine.get_pending_proposals()
        assert len(proposals) == 1
        assert proposals[0]["side"] == "sell"
        assert proposals[0]["symbol"] == "NVDA"
        assert proposals[0]["badge"] == "TAKE_PROFIT"
    finally:
        mcp_client.get_quote = original_get_quote
        mcp_client.paper.positions.clear()
        engine._pending_proposals.clear()


@pytest.mark.asyncio
async def test_anomalous_quote_does_not_trigger_phantom_stop_loss():
    """Verify an anomalous quote (e.g. -70% drop or fallback 100.0) is rejected and does not sell position."""
    from src.backend.mcp_client import mcp_client
    settings.execution_mode = "autonomous"
    settings.stop_loss_percent = 2.0

    # Seed AAPL at $331.00
    mcp_client.paper.positions["AAPL"] = {
        "quantity": 2.0,
        "average_buy_price": 331.00,
    }

    # Simulate quote returning corrupted/fallback $100.00 (-69.8%)
    async def mock_get_quote(symbol):
        return {"symbol": "AAPL", "last_trade_price": 100.0, "previous_close": 331.0}

    original_get_quote = mcp_client.get_quote
    mcp_client.get_quote = mock_get_quote

    try:
        exits = await engine.evaluate_open_positions()
        # Anomaly guard must abort the liquidation!
        assert len(exits) == 0
        # Position must remain intact
        assert "AAPL" in mcp_client.paper.positions
    finally:
        mcp_client.get_quote = original_get_quote
        mcp_client.paper.positions.clear()


@pytest.mark.asyncio
async def test_market_closed_blocks_execution_cycle_when_after_hours_disabled():
    """Verify execution cycle enters standby when market is closed and allow_after_hours is False."""
    from unittest.mock import patch
    settings.allow_after_hours = False

    with patch("src.backend.engine.is_market_open", return_value=False):
        await engine.execute_cycle()
        # No proposals or trades should be created
        assert len(engine.get_pending_proposals()) == 0
        assert len(risk_guard.get_trade_history()) == 0
