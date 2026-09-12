"""Unit tests for autonomous engine and proposal approval workflow."""

import pytest
from src.backend.config import settings
from src.backend.engine import engine
from src.backend.guardrails import risk_guard


@pytest.fixture(autouse=True)
def reset_engine():
    engine.stop()
    risk_guard.reset()
    settings.execution_mode = "autonomous"
    settings.dry_run = True
    yield
    engine.stop()
    risk_guard.reset()


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
