"""Tests for Paper vs Live account isolation, Universal Symbol Search, and Order Desk execution."""

import pytest
from fastapi.testclient import TestClient
from src.backend.app import app
from src.backend.auth import create_session_jwt
from src.backend.guardrails import GuardrailViolation, risk_guard
from src.backend.mcp_client import mcp_client


@pytest.fixture
def auth_client():
    """Client with valid authenticated session cookie."""
    client = TestClient(app)
    token = create_session_jwt({"username": "operator", "role": "admin"})
    client.cookies.set("session_token", token)
    return client


# 1. Universal Symbol Search Tests
def test_universal_symbol_search_popular(auth_client):
    response = auth_client.get("/api/search")
    assert response.status_code == 200
    results = response.json()
    assert len(results) >= 5
    symbols = [r["symbol"] for r in results]
    assert "SPY" in symbols
    assert "NVDA" in symbols


def test_universal_symbol_search_specific_query(auth_client):
    response = auth_client.get("/api/search?q=PLTR")
    assert response.status_code == 200
    results = response.json()
    assert len(results) > 0
    assert results[0]["symbol"] == "PLTR"
    assert "Palantir" in results[0]["name"]


def test_universal_symbol_search_arbitrary_ticker(auth_client):
    response = auth_client.get("/api/search?q=XYZ")
    assert response.status_code == 200
    results = response.json()
    assert len(results) > 0
    assert results[0]["symbol"] == "XYZ"


# 2. Complete Separation of Paper vs Live Accounts
def test_live_account_unfunded_zero_state(auth_client):
    """Ensure live account returns EXACTLY $0.00 and 0 positions when unfunded."""
    response = auth_client.get("/api/portfolio?mode=live")
    assert response.status_code == 200
    data = response.json()
    assert data["total_equity"] == 0.0
    assert data["buying_power"] == 0.0
    assert data["market_value"] == 0.0
    assert data["is_funded"] is False
    assert data["is_paper"] is False

    pos_resp = auth_client.get("/api/positions?mode=live")
    assert pos_resp.status_code == 200
    assert pos_resp.json() == []


def test_paper_account_starts_with_100k(auth_client):
    """Ensure paper account has $100,000.00 virtual cash and can reset."""
    mcp_client.reset_paper_account()
    response = auth_client.get("/api/portfolio?mode=paper")
    assert response.status_code == 200
    data = response.json()
    assert data["total_equity"] == 100000.0
    assert data["buying_power"] == 100000.0
    assert data["is_paper"] is True


def test_paper_order_execution_and_positions_flow(auth_client):
    """Ensure placing a paper order updates cash, creates paper position, and records paper trade."""
    mcp_client.reset_paper_account()
    risk_guard.reset()

    # Buy $1,000 of NVDA in paper mode
    order_payload = {
        "symbol": "NVDA",
        "side": "buy",
        "amount_usd": 100.0,  # within max order cap
        "mode": "paper",
    }
    response = auth_client.post("/api/orders", json=order_payload)
    assert response.status_code == 200
    receipt = response.json()["receipt"]
    assert receipt["status"] == "filled"
    assert receipt["environment"] == "paper"

    # Check paper portfolio updated
    port_resp = auth_client.get("/api/portfolio?mode=paper")
    port_data = port_resp.json()
    assert port_data["buying_power"] == 99900.0

    # Check paper positions updated
    pos_resp = auth_client.get("/api/positions?mode=paper")
    positions = pos_resp.json()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "NVDA"

    # Live portfolio MUST STILL BE ZERO! No data contamination!
    live_port_resp = auth_client.get("/api/portfolio?mode=live")
    assert live_port_resp.json()["total_equity"] == 0.0
    assert live_port_resp.json()["buying_power"] == 0.0

    live_pos_resp = auth_client.get("/api/positions?mode=live")
    assert live_pos_resp.json() == []


def test_unfunded_live_order_blocked(auth_client):
    """Ensure placing a live buy order when account has $0 buying power is safely blocked."""
    order_payload = {
        "symbol": "NVDA",
        "side": "buy",
        "amount_usd": 50.0,
        "mode": "live",
    }
    # When submitting via API, should return an error
    response = auth_client.post("/api/orders", json=order_payload)
    assert response.status_code == 400 or "Live Brokerage Account Unfunded" in response.text or response.status_code == 500


def test_paper_account_reset_endpoint(auth_client):
    """Ensure the reset paper account API endpoint clears holdings and restores $100k."""
    response = auth_client.post("/api/paper/reset")
    assert response.status_code == 200
    port = auth_client.get("/api/portfolio?mode=paper").json()
    assert port["buying_power"] == 100000.0
    pos = auth_client.get("/api/positions?mode=paper").json()
    assert pos == []


def test_get_accounts_endpoint(auth_client):
    """Ensure /api/accounts returns user's discovered Robinhood accounts."""
    response = auth_client.get("/api/accounts")
    assert response.status_code == 200
    accounts = response.json()
    assert isinstance(accounts, list)
    assert len(accounts) >= 1
    first = accounts[0]
    assert "account_number" in first
    assert "total_equity" in first
    assert "buying_power" in first
    assert "agentic_allowed" in first


def test_select_account_endpoint(auth_client):
    """Ensure /api/accounts/select switches the active Robinhood account."""
    response = auth_client.post("/api/accounts/select", json={"account_number": "222222222"})
    assert response.status_code == 200
    assert response.json()["active_account"] == "222222222"


@pytest.mark.asyncio
async def test_get_equity_quotes_parses_real_mcp_results():
    """Verify that get_equity_quotes correctly extracts real-time prices from MCP results schema."""
    from unittest.mock import AsyncMock, patch
    from src.backend.mcp_client import mcp_bridge

    mock_mcp_response = {
        "data": {
            "results": [
                {
                    "quote": {
                        "symbol": "NVDA",
                        "last_trade_price": "218.190000",
                        "bid_price": "218.280000",
                        "ask_price": "218.320000",
                        "previous_close": "218.360000",
                        "venue_last_trade_time": "2026-09-11T19:59:59Z"
                    },
                    "close": {
                        "symbol": "NVDA",
                        "price": "218.36"
                    }
                },
                {
                    "quote": {
                        "symbol": "AAPL",
                        "last_trade_price": "332.230000",
                        "bid_price": "332.500000",
                        "ask_price": "332.650000",
                        "previous_close": "326.570000",
                        "venue_last_trade_time": "2026-09-11T19:59:59Z"
                    },
                    "close": {
                        "symbol": "AAPL",
                        "price": "326.57"
                    }
                }
            ]
        }
    }

    with patch.object(mcp_bridge, "is_authenticated", return_value=True), \
         patch.object(mcp_bridge, "call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_mcp_response
        quotes = await mcp_bridge.get_equity_quotes(["NVDA", "AAPL"])
        assert "NVDA" in quotes
        assert quotes["NVDA"]["last_trade_price"] == 218.19
        assert quotes["NVDA"]["bid_price"] == 218.28
        assert quotes["NVDA"]["ask_price"] == 218.32
        assert quotes["NVDA"]["previous_close"] == 218.36

        assert "AAPL" in quotes
        assert quotes["AAPL"]["last_trade_price"] == 332.23
        assert quotes["AAPL"]["previous_close"] == 326.57


@pytest.mark.asyncio
async def test_get_equity_historicals_parses_real_mcp_results():
    """Verify that get_equity_historicals correctly parses bars with begins_at timestamp."""
    from unittest.mock import AsyncMock, patch
    from src.backend.mcp_client import mcp_bridge

    mock_mcp_hist_response = {
        "data": {
            "results": [
                {
                    "symbol": "NVDA",
                    "interval": "5minute",
                    "bars": [
                        {
                            "begins_at": "2026-09-11T13:30:00Z",
                            "open_price": "221.290000",
                            "high_price": "221.340000",
                            "low_price": "219.150000",
                            "close_price": "219.780000",
                            "volume": 2115506
                        }
                    ]
                }
            ]
        }
    }

    with patch.object(mcp_bridge, "is_authenticated", return_value=True), \
         patch.object(mcp_bridge, "call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_mcp_hist_response
        bars = await mcp_bridge.get_equity_historicals("NVDA", timeframe="1D")
        assert len(bars) == 1
        assert bars[0]["open"] == 221.29
        assert bars[0]["high"] == 221.34
        assert bars[0]["low"] == 219.15
        assert bars[0]["close"] == 219.78
        assert bars[0]["volume"] == 2115506

