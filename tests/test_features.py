"""Tests for new platform upgrades:
- TradingView Historicals API
- Multi-Agent Deliberation Council
- Options Engine & The Wheel Strategy
- Market Scanner
- Circuit Breakers & Trailing Stops
"""

import pytest
from fastapi.testclient import TestClient
from src.backend.app import app
from src.backend.auth import create_session_jwt
from src.backend.council import council
from src.backend.guardrails import GuardrailViolation, risk_guard
from src.backend.mcp_client import mcp_client
from src.backend.options_engine import options_engine
from src.backend.scanner import market_scanner


@pytest.fixture
def auth_client():
    """Client with valid authenticated session cookie."""
    client = TestClient(app)
    token = create_session_jwt({"username": "operator", "role": "admin"})
    client.cookies.set("session_token", token)
    return client


# --- Phase 1: Historicals API ---
@pytest.mark.asyncio
async def test_historicals_data_generation():
    bars = await mcp_client.get_historicals("SPY", timeframe="1D")
    assert len(bars) > 0
    assert "time" in bars[0]
    assert "open" in bars[0]
    assert "close" in bars[0]
    assert "volume" in bars[0]
    assert bars[0]["high"] >= bars[0]["low"]


def test_historicals_api_endpoint(auth_client):
    response = auth_client.get("/api/historicals/NVDA?timeframe=1D")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0


# --- Phase 2: Multi-Agent Deliberation Council ---
@pytest.mark.asyncio
async def test_deliberation_council():
    delib = await council.deliberate(
        symbol="NVDA",
        side="buy",
        amount_usd=50.0,
        strategy_rationale="Dip buyer test",
        current_price=128.40,
        portfolio_equity=12000.0,
        available_cash=4000.0,
    )
    assert delib["symbol"] == "NVDA"
    assert delib["verdict"] in ("APPROVED", "VETOED")
    assert "bull_argument" in delib
    assert "bear_argument" in delib
    assert "risk_officer_judgment" in delib
    assert 0 <= delib["consensus_score"] <= 100


def test_council_history_api(auth_client):
    response = auth_client.get("/api/council/history")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# --- Phase 3: Options Engine & Wheel Strategy ---
@pytest.mark.asyncio
async def test_option_chain_generation():
    chain = await options_engine.get_option_chain("NVDA")
    assert chain["symbol"] == "NVDA"
    assert len(chain["expirations"]) > 0
    assert len(chain["strikes"]) > 0
    first_strike = chain["strikes"][0]
    assert "call" in first_strike
    assert "put" in first_strike
    assert "delta" in first_strike["call"]
    assert "theta" in first_strike["call"]


@pytest.mark.asyncio
async def test_wheel_recommendations():
    recs = await options_engine.get_wheel_recommendations()
    assert isinstance(recs, list)
    assert len(recs) > 0
    first_rec = recs[0]
    assert "strategy_type" in first_rec
    assert "annualized_yield_pct" in first_rec


def test_options_order_api(auth_client):
    payload = {
        "symbol": "NVDA",
        "action": "sell_to_open",
        "contract_type": "put",
        "strike_price": 120.0,
        "expiration": "2026-04-10",
        "contracts": 1,
        "premium_usd": 150.0,
    }
    response = auth_client.post("/api/options/order", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["receipt"]["symbol"] == "NVDA"


# --- Phase 4: Market Scanner ---
@pytest.mark.asyncio
async def test_market_scanner():
    scans = await market_scanner.scan_market()
    assert isinstance(scans, list)


def test_market_scanner_api(auth_client):
    response = auth_client.get("/api/market/scanner")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# --- Phase 5: Circuit Breaker & Trailing Stop ---
def test_circuit_breaker_tripped():
    risk_guard.reset()
    assert not risk_guard._circuit_breaker_active

    # Drawdown of -4.5% trips the 3.0% circuit breaker
    risk_guard.check_portfolio_drawdown(-4.5)
    assert risk_guard._circuit_breaker_active

    # Buy orders should now be blocked
    with pytest.raises(GuardrailViolation, match="Circuit Breaker Active"):
        risk_guard.validate_order(symbol="NVDA", side="buy", amount_usd=50.0)

    # Reset circuit breaker
    risk_guard.reset_circuit_breaker()
    assert not risk_guard._circuit_breaker_active

    # Order should now validate
    risk_guard.validate_order(symbol="NVDA", side="buy", amount_usd=50.0)


def test_trailing_stop_logic():
    risk_guard.reset()
    symbol = "NVDA"
    # Price rises to 150
    risk_guard.update_peak_price(symbol, 150.0)

    # Current price at 148 (1.33% drop) -> does not trigger 2.5% trail
    assert not risk_guard.should_trigger_trailing_stop(symbol, 148.0, trail_percent=2.5)

    # Current price drops to 145 (3.33% drop from 150) -> triggers trail!
    assert risk_guard.should_trigger_trailing_stop(symbol, 145.0, trail_percent=2.5)


# --- Phase 6: Conversational Copilot Queries ---
@pytest.mark.asyncio
async def test_copilot_quote_query():
    from src.backend.agent import trading_agent
    reply = await trading_agent.chat("What is NVDA trading at?", [])
    assert "NVDA" in reply
    assert "Real-Time Quote" in reply or "Quote" in reply


@pytest.mark.asyncio
async def test_copilot_momentum_evaluation():
    from src.backend.agent import trading_agent
    reply = await trading_agent.chat("Evaluate current market momentum for NVDA", [])
    assert "Momentum" in reply
    assert "NVDA" in reply
    assert "RSI" in reply


@pytest.mark.asyncio
async def test_copilot_portfolio_query():
    from src.backend.agent import trading_agent
    reply = await trading_agent.chat("What is our paper portfolio performance?", [])
    assert "Portfolio" in reply
    assert "Total Account Equity" in reply


def test_copilot_chat_api(auth_client):
    response = auth_client.post("/api/chat", json={"message": "What is NVDA trading at?"})
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data
    assert "NVDA" in data["reply"]


def test_market_status_api(auth_client):
    response = auth_client.get("/api/market/status")
    assert response.status_code == 200
    data = response.json()
    assert "is_open" in data
    assert "session" in data
    assert "status_text" in data
    assert "current_time_et" in data
    assert "next_open_et" in data
    assert data["allow_after_hours"] is False


def test_get_strategies_api(auth_client):
    response = auth_client.get("/api/strategies")
    assert response.status_code == 200
    data = response.json()
    assert "active_strategy" in data
    assert "strategies" in data
    strategy_ids = [s["id"] for s in data["strategies"]]
    assert "momentum_dip_buyer" in strategy_ids
    assert "smart_dca" in strategy_ids
    assert "orb_breakout" in strategy_ids
    assert "rvol_squeeze" in strategy_ids
    assert "directional_options" in strategy_ids
    assert "zero_dte_scalper" in strategy_ids
    assert "pairs_divergence" in strategy_ids


def test_switch_active_strategy(auth_client):
    response = auth_client.post("/api/settings", json={"active_strategy": "orb_breakout"})
    assert response.status_code == 200
    data = response.json()
    assert data["engine"]["active_strategy"] == "orb_breakout"

    status_resp = auth_client.get("/api/engine/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["active_strategy"] == "orb_breakout"


@pytest.mark.asyncio
async def test_directional_options_contract_selection():
    contract = await options_engine.get_directional_contract("NVDA", side="bullish")
    assert contract is not None
    assert contract["symbol"] == "NVDA"
    assert contract["contract_type"] == "call"
    assert 0.50 <= contract["delta"] <= 0.85
    assert contract["premium_usd"] > 0


@pytest.mark.asyncio
async def test_zero_dte_contract_selection():
    contract = await options_engine.get_zero_dte_contract("SPY", side="call")
    assert contract is not None
    assert contract["symbol"] == "SPY"
    assert contract["contract_type"] == "call"
    assert contract["is_zero_dte"] is True
    assert contract["max_risk_cap_usd"] == 50.0


@pytest.mark.asyncio
async def test_orb_breakout_strategy_evaluate():
    from src.backend.strategies import AVAILABLE_STRATEGIES
    strat = AVAILABLE_STRATEGIES["orb_breakout"]
    recs = await strat.evaluate()
    assert isinstance(recs, list)
    if recs:
        assert recs[0]["strategy"] == "orb_breakout"
        assert recs[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_rvol_squeeze_strategy_evaluate():
    from src.backend.strategies import AVAILABLE_STRATEGIES
    strat = AVAILABLE_STRATEGIES["rvol_squeeze"]
    recs = await strat.evaluate()
    assert isinstance(recs, list)
    if recs:
        assert recs[0]["strategy"] == "rvol_squeeze"


@pytest.mark.asyncio
async def test_zero_dte_scalper_strategy_evaluate():
    from src.backend.strategies import AVAILABLE_STRATEGIES
    strat = AVAILABLE_STRATEGIES["zero_dte_scalper"]
    recs = await strat.evaluate()
    assert isinstance(recs, list)
    if recs:
        assert recs[0]["is_option"] is True


@pytest.mark.asyncio
async def test_pairs_divergence_strategy_evaluate():
    from src.backend.strategies import AVAILABLE_STRATEGIES
    strat = AVAILABLE_STRATEGIES["pairs_divergence"]
    recs = await strat.evaluate()
    assert isinstance(recs, list)



