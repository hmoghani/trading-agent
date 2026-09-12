"""Autonomous trading background engine with live SSE event broadcasting."""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid
from src.backend.config import settings
from src.backend.guardrails import GuardrailViolation, risk_guard
from src.backend.mcp_client import mcp_client
from src.backend.strategies import AVAILABLE_STRATEGIES, BaseStrategy

logger = logging.getLogger(__name__)


class AutonomousTradingEngine:
    """Manages scheduled strategy execution loops, proposals, and live event broadcasts."""

    def __init__(self) -> None:
        self.is_running: bool = False
        self.active_strategy_name: str = "momentum_dip_buyer"
        self._task: Optional[asyncio.Task] = None
        self._subscribers: List[asyncio.Queue] = []
        self._pending_proposals: Dict[str, Dict[str, Any]] = {}
        self._activity_logs: List[Dict[str, Any]] = []

    def get_status(self) -> Dict[str, Any]:
        """Return engine operational status."""
        strat = AVAILABLE_STRATEGIES.get(self.active_strategy_name)
        return {
            "is_running": self.is_running,
            "active_strategy": self.active_strategy_name,
            "strategy_metadata": strat.get_metadata() if strat else None,
            "execution_mode": settings.execution_mode,
            "interval_seconds": settings.strategy_interval_seconds,
            "pending_proposals_count": len(self._pending_proposals),
            "recent_logs_count": len(self._activity_logs),
        }

    async def broadcast_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Send real-time SSE event to all connected web dashboard clients."""
        payload = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        self._activity_logs.insert(0, payload)
        if len(self._activity_logs) > 200:
            self._activity_logs.pop()

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        """Register a new dashboard SSE subscriber."""
        queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unregister an SSE subscriber."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def get_recent_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent activity event logs."""
        return list(self._activity_logs[:limit])

    def get_pending_proposals(self) -> List[Dict[str, Any]]:
        """Return list of unapproved trade proposals."""
        return list(self._pending_proposals.values())

    async def approve_proposal(self, proposal_id: str) -> Dict[str, Any]:
        """User manual approval for a proposal in supervised mode."""
        proposal = self._pending_proposals.pop(proposal_id, None)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found.")

        # Execute trade
        receipt = await mcp_client.place_order(
            symbol=proposal["symbol"],
            side=proposal["side"],
            amount_usd=proposal["amount_usd"],
            rationale=proposal.get("rationale", "User approved supervised proposal."),
        )
        await self.broadcast_event(
            "trade_executed",
            {
                "mode": "supervised",
                "proposal_id": proposal_id,
                "receipt": receipt,
                "message": f"Supervised trade executed: {proposal['side'].upper()} {proposal['symbol']} (${proposal['amount_usd']:.2f})",
            },
        )
        return receipt

    async def reject_proposal(self, proposal_id: str, reason: str = "Rejected by user") -> Dict[str, Any]:
        """User manual rejection for a proposal."""
        proposal = self._pending_proposals.pop(proposal_id, None)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found.")

        await self.broadcast_event(
            "proposal_rejected",
            {
                "proposal_id": proposal_id,
                "symbol": proposal["symbol"],
                "reason": reason,
            },
        )
        return {"status": "rejected", "proposal_id": proposal_id}

    async def execute_cycle(self) -> None:
        """Execute a single strategy evaluation pass during Regular Trading Hours."""
        strategy = AVAILABLE_STRATEGIES.get(self.active_strategy_name)
        if not strategy:
            logger.warning(f"Unknown strategy: {self.active_strategy_name}")
            return

        # Regular Trading Hours (RTH) Enforcement for Live Brokerage Orders
        from src.backend.market_hours import is_market_open, get_market_status
        if not settings.dry_run and not is_market_open():
            status = get_market_status()
            logger.info(f"Market Closed ({status['status_text']}). Autonomous engine standing by for Live trading.")
            await self.broadcast_event(
                "market_closed_standby",
                {
                    "message": f"Market Closed ({status['status_text']}). Live engine in standby until next open on {status['next_open_et']}.",
                    "current_time_et": status["current_time_et"],
                    "next_open_et": status["next_open_et"],
                    "session": status["session"],
                },
            )
            return

        await self.broadcast_event(
            "scan_started",
            {
                "strategy": strategy.name,
                "whitelisted_symbols": settings.whitelisted_symbols,
                "message": f"Scanning {len(settings.whitelisted_symbols)} whitelisted assets using {strategy.name}...",
            },
        )

        try:
            recommendations = await strategy.evaluate()
        except Exception as e:
            logger.error(f"Strategy evaluation error: {e}")
            await self.broadcast_event(
                "scan_error",
                {"error": str(e), "strategy": strategy.name},
            )
            return

        if not recommendations:
            await self.broadcast_event(
                "scan_completed",
                {
                    "strategy": strategy.name,
                    "action_taken": False,
                    "message": "Market scan complete. No signals met strategy entry criteria.",
                },
            )
            return

        # Process each recommendation
        for rec in recommendations:
            symbol = rec["symbol"]
            side = rec["side"]
            amount_usd = rec["amount_usd"]
            rationale = rec.get("rationale", "")

            try:
                # Pre-validate guardrails
                risk_guard.validate_order(symbol=symbol, side=side, amount_usd=amount_usd, is_live=not settings.dry_run)
            except GuardrailViolation as gv:
                logger.warning(f"Guardrail intercepted trade for {symbol}: {gv}")
                await self.broadcast_event(
                    "guardrail_blocked",
                    {
                        "symbol": symbol,
                        "side": side,
                        "amount_usd": amount_usd,
                        "violation": str(gv),
                    },
                )
                continue

            # Multi-Agent Deliberation Council (Bull vs Bear vs Risk Officer)
            from src.backend.council import council
            portfolio = await mcp_client.get_portfolio()
            risk_guard.check_portfolio_drawdown(float(portfolio.get("day_return_percent", 0.0)))
            quote = await mcp_client.get_quote(symbol)
            deliberation = await council.deliberate(
                symbol=symbol,
                side=side,
                amount_usd=amount_usd,
                strategy_rationale=rationale,
                current_price=float(quote.get("last_trade_price", 100.0)),
                portfolio_equity=float(portfolio.get("total_equity", 10000.0)),
                available_cash=float(portfolio.get("buying_power", 5000.0)),
            )
            await self.broadcast_event("council_deliberation", deliberation)

            if deliberation.get("verdict") != "APPROVED":
                logger.info(f"Deliberation Council vetoed {side.upper()} {symbol}: {deliberation.get('risk_officer_judgment')}")
                await self.broadcast_event("trade_vetoed", {
                    "symbol": symbol,
                    "side": side,
                    "deliberation": deliberation,
                    "message": f"Council Vetoed {symbol} (Conviction {deliberation.get('consensus_score')}%)",
                })
                continue

            # Execution branching based on mode
            if settings.execution_mode == "autonomous":
                # Fully hands-free execution!
                receipt = await mcp_client.place_order(
                    symbol=symbol,
                    side=side,
                    amount_usd=amount_usd,
                    rationale=f"Council Approved ({deliberation.get('consensus_score')}%): {rationale}",
                )
                await self.broadcast_event(
                    "trade_executed",
                    {
                        "mode": "autonomous",
                        "symbol": symbol,
                        "side": side,
                        "amount_usd": amount_usd,
                        "receipt": receipt,
                        "message": f"[AUTONOMOUS] Executed {side.upper()} {symbol} for ${amount_usd:.2f}",
                    },
                )
            else:
                # Supervised mode: create proposal card for user approval
                proposal_id = f"prop-{uuid.uuid4().hex[:8]}"
                proposal = {
                    "id": proposal_id,
                    "symbol": symbol,
                    "side": side,
                    "amount_usd": amount_usd,
                    "rationale": rationale,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                self._pending_proposals[proposal_id] = proposal
                await self.broadcast_event(
                    "trade_proposal_created",
                    {
                        "mode": "supervised",
                        "proposal": proposal,
                        "message": f"[SUPERVISED] New trade recommendation for {symbol}. Awaiting your approval.",
                    },
                )

    async def _loop(self) -> None:
        """Background continuous execution loop."""
        logger.info("Autonomous trading loop started.")
        while self.is_running:
            try:
                await self.execute_cycle()
            except Exception as e:
                logger.error(f"Error in autonomous execution loop: {e}")
            await asyncio.sleep(settings.strategy_interval_seconds)

    def start(self) -> None:
        """Start autonomous trading loop."""
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("Autonomous engine activated.")

    def stop(self) -> None:
        """Stop autonomous trading loop."""
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Autonomous engine stopped.")


engine = AutonomousTradingEngine()
