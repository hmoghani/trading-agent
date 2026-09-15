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

    async def evaluate_open_positions(self) -> List[Dict[str, Any]]:
        """Active Position Lifecycle & Profit-Taking Manager:
        Evaluates open positions in Live or Paper trading against:
        1. Take-Profit (TP): Gain >= settings.take_profit_percent (e.g. +3.0%)
        2. Stop-Loss (SL): Loss <= -settings.stop_loss_percent (e.g. -2.0%)
        3. Trailing-Stop: Price pulled back >= settings.trailing_stop_percent (e.g. 1.8%) from high-water mark after being in profit
        
        Returns list of executed or proposed exit actions.
        """
        exits: List[Dict[str, Any]] = []
        try:
            positions = await mcp_client.get_positions()
        except Exception as e:
            logger.warning(f"Failed to fetch positions for lifecycle evaluation: {e}")
            return []

        if not positions:
            return []

        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue

            quantity = float(pos.get("quantity", 0.0))
            if quantity <= 0:
                continue

            avg_buy = float(pos.get("average_buy_price", 0.0))
            current_price = float(pos.get("current_price", 0.0))
            market_value = float(pos.get("market_value", 0.0))

            # Fetch fresh real-time quote if current_price is 0
            if current_price <= 0:
                try:
                    q = await mcp_client.get_quote(symbol)
                    current_price = float(q.get("last_trade_price", 0.0))
                    market_value = round(quantity * current_price, 2)
                except Exception:
                    pass

            if current_price <= 0 or avg_buy <= 0:
                continue

            # Calculate exact unrealized PnL % and USD
            pnl_pct = ((current_price - avg_buy) / avg_buy) * 100
            pnl_usd = (current_price - avg_buy) * quantity

            # Update peak price for trailing stop
            risk_guard.update_peak_price(symbol, current_price)

            exit_reason = None
            exit_badge = None

            # 1. Take-Profit Check: Lock in cash profit
            if pnl_pct >= settings.take_profit_percent:
                exit_reason = (
                    f"🎯 TAKE PROFIT TARGET HIT: {symbol} gained {pnl_pct:+.2f}% "
                    f"(${pnl_usd:+.2f}) from entry ${avg_buy:.2f} to ${current_price:.2f}. "
                    f"Locking in profits."
                )
                exit_badge = "TAKE_PROFIT"

            # 2. Stop-Loss Check: Limit downside and preserve capital
            elif pnl_pct <= -settings.stop_loss_percent:
                exit_reason = (
                    f"🛑 STOP LOSS TRIGGERED: {symbol} dropped {pnl_pct:+.2f}% "
                    f"(${pnl_usd:+.2f}) from entry ${avg_buy:.2f} to ${current_price:.2f}. "
                    f"Exiting to preserve capital."
                )
                exit_badge = "STOP_LOSS"

            # 3. Trailing-Stop Check: Protect accumulated gains from peak
            elif settings.enable_trailing_stop and pnl_pct > 0.5:
                if risk_guard.should_trigger_trailing_stop(symbol, current_price, settings.trailing_stop_percent):
                    peak_price = risk_guard._peak_prices.get(symbol, current_price)
                    pullback = ((peak_price - current_price) / peak_price) * 100
                    exit_reason = (
                        f"⚡ TRAILING STOP TRIGGERED: {symbol} pulled back {pullback:.2f}% "
                        f"from peak ${peak_price:.2f} to ${current_price:.2f} (Net: {pnl_pct:+.2f}%). "
                        f"Protecting accumulated profit."
                    )
                    exit_badge = "TRAILING_STOP"

            if exit_reason:
                trade_amount_usd = round(market_value, 2)
                if trade_amount_usd <= 0:
                    trade_amount_usd = round(quantity * current_price, 2)

                logger.info(f"[{exit_badge}] Exiting {symbol} ({quantity} shares @ ${current_price:.2f} = ${trade_amount_usd:.2f}): {exit_reason}")

                # Broadcast exit signal event to all live dashboard clients
                await self.broadcast_event(
                    "position_exit_triggered",
                    {
                        "symbol": symbol,
                        "badge": exit_badge,
                        "shares": quantity,
                        "price": current_price,
                        "pnl_percent": round(pnl_pct, 2),
                        "pnl_usd": round(pnl_usd, 2),
                        "amount_usd": trade_amount_usd,
                        "reason": exit_reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )

                if settings.execution_mode == "autonomous":
                    # Autonomous mode: execute SELL order immediately!
                    try:
                        receipt = await mcp_client.place_order(
                            symbol=symbol,
                            side="sell",
                            amount_usd=trade_amount_usd,
                            rationale=exit_reason,
                        )
                        risk_guard.clear_peak_price(symbol)
                        await self.broadcast_event(
                            "trade_executed",
                            {
                                "mode": "autonomous",
                                "symbol": symbol,
                                "side": "sell",
                                "badge": exit_badge,
                                "amount_usd": trade_amount_usd,
                                "receipt": receipt,
                                "message": f"[AUTONOMOUS {exit_badge}] Sold {symbol} for ${trade_amount_usd:.2f} ({pnl_pct:+.2f}% P&L)",
                            },
                        )
                        exits.append(receipt)
                    except Exception as ex:
                        logger.error(f"Failed to execute autonomous SELL for {symbol}: {ex}")
                else:
                    # Supervised mode: create proposal card for user approval
                    proposal_id = f"prop-{uuid.uuid4().hex[:8]}"
                    proposal = {
                        "id": proposal_id,
                        "symbol": symbol,
                        "side": "sell",
                        "badge": exit_badge,
                        "amount_usd": trade_amount_usd,
                        "rationale": exit_reason,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self._pending_proposals[proposal_id] = proposal
                    await self.broadcast_event(
                        "trade_proposal_created",
                        {
                            "mode": "supervised",
                            "proposal": proposal,
                            "message": f"[{exit_badge}] Sell recommendation for {symbol} ({pnl_pct:+.2f}%). Awaiting your approval.",
                        },
                    )

        return exits

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

        # STEP 1: Position Lifecycle Management (Take-Profit, Stop-Loss, Trailing-Stop)
        # Always evaluate open positions FIRST to lock in gains or cut losses!
        exits = await self.evaluate_open_positions()
        exited_symbols = {e.get("symbol") for e in exits if isinstance(e, dict) and e.get("symbol")}

        # STEP 2: Entry Strategy Evaluation (Scan for new high-probability entries)
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

            # Prevent immediate rebuy of a symbol that was just exited in this cycle
            if side == "buy" and symbol in exited_symbols:
                logger.info(f"Skipping BUY for {symbol}: Position was just exited in the current cycle.")
                continue

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
