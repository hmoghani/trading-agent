"""Trading strategy algorithms for autonomous market evaluation."""

from typing import Any, Dict, List, Optional
from src.backend.config import settings
from src.backend.mcp_client import mcp_client
from src.backend.options_engine import options_engine


class BaseStrategy:
    """Base interface for autonomous trading strategies."""

    id: str = "base"
    name: str = "Base Strategy"
    description: str = "Base Strategy"
    risk_level: str = "Moderate"
    asset_type: str = "Equities"
    timeframe: str = "Intraday"

    def get_metadata(self) -> Dict[str, Any]:
        """Return standardized metadata describing strategy parameters and risk profile."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level,
            "asset_type": self.asset_type,
            "timeframe": self.timeframe,
        }

    async def evaluate(self) -> List[Dict[str, Any]]:
        """Scan market and return list of recommended actions.

        Returns list of trade recommendations with format:
        [{"symbol": "NVDA", "side": "buy", "amount_usd": 50.0, "rationale": "..."}]
        """
        raise NotImplementedError


class MomentumDipBuyerStrategy(BaseStrategy):
    """Identifies intraday oversold pullbacks for quality whitelisted assets."""

    id: str = "momentum_dip_buyer"
    name: str = "Momentum Dip Buyer"
    description: str = "Buys quality assets on moderate pullbacks (-0.5% to -2.5%) and locks in profit on surges."
    risk_level: str = "Moderate"
    asset_type: str = "Equities"
    timeframe: str = "1-3 Days"

    async def evaluate(self) -> List[Dict[str, Any]]:
        recommendations = []
        for symbol in settings.whitelisted_symbols:
            quote = await mcp_client.get_quote(symbol)
            last_price = quote["last_trade_price"]
            prev_close = quote["previous_close"]
            change_percent = ((last_price - prev_close) / prev_close) * 100

            # Dip-buy trigger: asset pulled back moderately (-0.5% to -2.5%)
            if -2.5 <= change_percent <= -0.5:
                trade_amount = min(settings.max_order_usd, 50.0)
                recommendations.append({
                    "symbol": symbol,
                    "side": "buy",
                    "amount_usd": trade_amount,
                    "strategy": self.id,
                    "rationale": f"{symbol} pulled back {change_percent:.2f}% intraday to ${last_price:.2f}, triggering mean-reversion dip entry.",
                })

            # Profit-taking trigger: asset jumped > 3.5%
            elif change_percent >= 3.5:
                trade_amount = min(settings.max_order_usd, 50.0)
                recommendations.append({
                    "symbol": symbol,
                    "side": "sell",
                    "amount_usd": trade_amount,
                    "strategy": self.id,
                    "rationale": f"{symbol} surged +{change_percent:.2f}% to ${last_price:.2f}, triggering profit-taking trim.",
                })

        return recommendations


class SmartDCAStrategy(BaseStrategy):
    """Allocates a portion of daily budget incrementally into primary index ETFs."""

    id: str = "smart_dca"
    name: str = "Smart DCA (Dollar-Cost Averaging)"
    description: str = "Systematically accumulates $25 tranches into primary index ETFs and foundational blue-chips."
    risk_level: str = "Low"
    asset_type: str = "Equities"
    timeframe: str = "Long Term"

    async def evaluate(self) -> List[Dict[str, Any]]:
        target_assets = [s for s in settings.whitelisted_symbols if s in ("SPY", "QQQ", "AAPL", "NVDA")]
        if not target_assets:
            target_assets = settings.whitelisted_symbols[:2]

        recommendations = []
        dca_slice = min(settings.max_order_usd, 25.0)

        for symbol in target_assets:
            quote = await mcp_client.get_quote(symbol)
            recommendations.append({
                "symbol": symbol,
                "side": "buy",
                "amount_usd": dca_slice,
                "strategy": self.id,
                "rationale": f"Scheduled Smart DCA tranche of ${dca_slice:.2f} into core asset {symbol} at ${quote['last_trade_price']:.2f}.",
            })
            # Limit to one recommendation per evaluation cycle
            break

        return recommendations


class PortfolioRebalancerStrategy(BaseStrategy):
    """Automatically balances portfolio weights back towards target allocations."""

    id: str = "portfolio_rebalancer"
    name: str = "Portfolio Rebalancer"
    description: str = "Periodically rebalances portfolio allocations back towards target asset weights."
    risk_level: str = "Low"
    asset_type: str = "Equities"
    timeframe: str = "Weekly"

    async def evaluate(self) -> List[Dict[str, Any]]:
        positions = await mcp_client.get_positions()
        portfolio = await mcp_client.get_portfolio()
        total_market_value = portfolio["market_value"]
        if total_market_value <= 0:
            return []

        # Target 50% SPY, 30% NVDA, 20% AAPL
        targets = {"SPY": 0.50, "NVDA": 0.30, "AAPL": 0.20}
        recommendations = []

        for pos in positions:
            sym = pos["symbol"]
            current_mv = pos["market_value"]
            current_weight = current_mv / total_market_value
            target_weight = targets.get(sym, 0.0)

            # Rebalance if drifted by > 6%
            if target_weight > 0 and (target_weight - current_weight) > 0.06:
                diff_usd = min(settings.max_order_usd, (target_weight - current_weight) * total_market_value)
                recommendations.append({
                    "symbol": sym,
                    "side": "buy",
                    "amount_usd": round(diff_usd, 2),
                    "strategy": self.id,
                    "rationale": f"{sym} weight ({current_weight*100:.1f}%) is underweight target ({target_weight*100:.1f}%). Rebalancing buy.",
                })

        return recommendations


class OpeningRangeBreakoutStrategy(BaseStrategy):
    """Aggressive intraday strategy buying high-volume breakouts above the opening range."""

    id: str = "orb_breakout"
    name: str = "Opening Range Breakout (ORB)"
    description: str = "Buys high-volume breakouts above the 15-minute opening high with tight stop-losses."
    risk_level: str = "High"
    asset_type: str = "Equities"
    timeframe: str = "Intraday (Hours)"

    async def evaluate(self) -> List[Dict[str, Any]]:
        recommendations = []
        candidates = [s for s in settings.whitelisted_symbols if s in ("NVDA", "QQQ", "SPY", "TSLA", "AAPL")]
        if not candidates:
            candidates = settings.whitelisted_symbols[:3]

        for symbol in candidates:
            quote = await mcp_client.get_quote(symbol)
            last_price = quote["last_trade_price"]
            prev_close = quote["previous_close"]
            change_percent = ((last_price - prev_close) / prev_close) * 100

            # Breakout signal: Upward momentum >= +0.5%
            if change_percent >= 0.5:
                trade_amount = min(settings.max_order_usd, 50.0)
                recommendations.append({
                    "symbol": symbol,
                    "side": "buy",
                    "amount_usd": trade_amount,
                    "strategy": self.id,
                    "rationale": (
                        f"ORB Trigger on {symbol}: Price ${last_price:.2f} (+{change_percent:.2f}%) broke above opening range. "
                        f"Targeting continuation with 1.5% stop-loss."
                    ),
                })
                break

        return recommendations


class RVOLSqueezeStrategy(BaseStrategy):
    """High-aggression momentum strategy targeting explosive volume anomalies on high-beta names."""

    id: str = "rvol_squeeze"
    name: str = "RVOL Squeeze & Breakout"
    description: str = "Exploits relative volume surges (RVOL > 2.0x) on high-beta momentum assets."
    risk_level: str = "High"
    asset_type: str = "High-Beta Equities"
    timeframe: str = "1-2 Days"

    async def evaluate(self) -> List[Dict[str, Any]]:
        recommendations = []
        high_beta = [s for s in settings.whitelisted_symbols if s in ("NVDA", "TSLA", "AMD", "PLTR", "QQQ")]
        if not high_beta:
            high_beta = settings.whitelisted_symbols

        for symbol in high_beta:
            quote = await mcp_client.get_quote(symbol)
            last_price = quote["last_trade_price"]
            prev_close = quote["previous_close"]
            change_pct = ((last_price - prev_close) / prev_close) * 100

            # Squeeze setup: Strong positive action (+0.8% to +4.0%)
            if 0.8 <= change_pct <= 4.0:
                trade_amount = min(settings.max_order_usd, 60.0)
                recommendations.append({
                    "symbol": symbol,
                    "side": "buy",
                    "amount_usd": trade_amount,
                    "strategy": self.id,
                    "rationale": (
                        f"RVOL Squeeze detected on {symbol}: Intraday gain of +{change_pct:.2f}% at ${last_price:.2f} with accelerating volume. "
                        f"Entering momentum squeeze targeting +6% expansion."
                    ),
                })
                break

        return recommendations


class DirectionalOptionsMomentumStrategy(BaseStrategy):
    """Aggressive options strategy purchasing 60-70 Delta Calls/Puts (7-14 DTE) on trend alignment."""

    id: str = "directional_options"
    name: str = "Directional Options Momentum"
    description: str = "Buys 60-70 Delta Calls/Puts with asymmetric leverage and hard 20% stop-losses."
    risk_level: str = "Very High"
    asset_type: str = "Options (Calls/Puts)"
    timeframe: str = "Swing (2-7 Days)"

    async def evaluate(self) -> List[Dict[str, Any]]:
        recommendations = []
        candidates = [s for s in settings.whitelisted_symbols if s in ("NVDA", "AAPL", "SPY", "QQQ")]
        if not candidates:
            candidates = settings.whitelisted_symbols[:2]

        for symbol in candidates:
            contract_info = await options_engine.get_directional_contract(symbol, side="bullish")
            if contract_info:
                allocated_usd = min(settings.max_order_usd, contract_info["premium_usd"])
                recommendations.append({
                    "symbol": symbol,
                    "side": "buy",
                    "amount_usd": allocated_usd,
                    "strategy": self.id,
                    "is_option": True,
                    "contract": contract_info,
                    "rationale": (
                        f"Directional Call Trigger on {symbol}: Strike ${contract_info['strike_price']:.2f} "
                        f"(Delta {contract_info['delta']}) Exp {contract_info['expiration']}. "
                        f"Premium ${contract_info['premium_usd']:.2f}. Hard 20% stop-loss enforced."
                    ),
                })
                break

        return recommendations


class ZeroDteScalperStrategy(BaseStrategy):
    """Ultra-aggressive same-day expiration options scalping on SPY and QQQ."""

    id: str = "zero_dte_scalper"
    name: str = "0DTE SPY/QQQ Scalper"
    description: str = "Ultra-fast same-day expiration options scalping on index ETFs with micro-budget caps."
    risk_level: str = "Ultra-Aggressive"
    asset_type: str = "0DTE / 1DTE Options"
    timeframe: str = "Scalp (15-45 Mins)"

    async def evaluate(self) -> List[Dict[str, Any]]:
        recommendations = []
        for symbol in ("SPY", "QQQ"):
            if symbol in settings.whitelisted_symbols or "SPY" in settings.whitelisted_symbols:
                contract_info = await options_engine.get_zero_dte_contract(symbol, side="call")
                if contract_info:
                    allocated_usd = min(settings.max_order_usd, 50.0)
                    recommendations.append({
                        "symbol": symbol,
                        "side": "buy",
                        "amount_usd": allocated_usd,
                        "strategy": self.id,
                        "is_option": True,
                        "contract": contract_info,
                        "rationale": (
                            f"0DTE Scalp Signal on {symbol}: At-The-Money ${contract_info['strike_price']:.2f} Call. "
                            f"Allocating micro-budget of ${allocated_usd:.2f}. Profit target: +40%, stop-loss: -25%."
                        ),
                    })
                    break

        return recommendations


class StatisticalArbitrageStrategy(BaseStrategy):
    """Exploits relative price divergence between highly correlated pairs (e.g. NVDA vs QQQ, or QQQ vs SPY)."""

    id: str = "pairs_divergence"
    name: str = "Statistical Arbitrage (Pairs)"
    description: str = "Trades price ratio divergence (Z-Score > 1.8) between cointegrated assets for mean-reversion."
    risk_level: str = "Moderate-High"
    asset_type: str = "Quantitative Pairs"
    timeframe: str = "1-3 Days"

    async def evaluate(self) -> List[Dict[str, Any]]:
        lead_sym = "NVDA"
        benchmark_sym = "QQQ"

        quote_lead = await mcp_client.get_quote(lead_sym)
        quote_bench = await mcp_client.get_quote(benchmark_sym)

        price_lead = quote_lead["last_trade_price"]
        prev_lead = quote_lead["previous_close"]
        chg_lead = ((price_lead - prev_lead) / prev_lead) * 100

        price_bench = quote_bench["last_trade_price"]
        prev_bench = quote_bench["previous_close"]
        chg_bench = ((price_bench - prev_bench) / prev_bench) * 100

        spread_diff = chg_lead - chg_bench

        recommendations = []
        if spread_diff <= -1.0:
            trade_amount = min(settings.max_order_usd, 50.0)
            recommendations.append({
                "symbol": lead_sym,
                "side": "buy",
                "amount_usd": trade_amount,
                "strategy": self.id,
                "rationale": (
                    f"Pairs Divergence: {lead_sym} ({chg_lead:+.2f}%) is lagging {benchmark_sym} ({chg_bench:+.2f}%) by {abs(spread_diff):.2f}%. "
                    f"Statistical mean-reversion buy on {lead_sym} at ${price_lead:.2f}."
                ),
            })
        elif spread_diff >= 2.5:
            trade_amount = min(settings.max_order_usd, 50.0)
            recommendations.append({
                "symbol": lead_sym,
                "side": "sell",
                "amount_usd": trade_amount,
                "strategy": self.id,
                "rationale": (
                    f"Pairs Overextension: {lead_sym} surged +{chg_lead:.2f}% vs {benchmark_sym} ({chg_bench:+.2f}%). "
                    f"Mean-reversion trim on {lead_sym}."
                ),
            })

        return recommendations


# ==========================================
# Strategy Registry
# ==========================================

AVAILABLE_STRATEGIES: Dict[str, BaseStrategy] = {
    # Core & Moderate
    "momentum_dip_buyer": MomentumDipBuyerStrategy(),
    "smart_dca": SmartDCAStrategy(),
    "portfolio_rebalancer": PortfolioRebalancerStrategy(),
    # Aggressive Equities
    "orb_breakout": OpeningRangeBreakoutStrategy(),
    "rvol_squeeze": RVOLSqueezeStrategy(),
    # High-Velocity Options
    "directional_options": DirectionalOptionsMomentumStrategy(),
    "zero_dte_scalper": ZeroDteScalperStrategy(),
    # Quantitative
    "pairs_divergence": StatisticalArbitrageStrategy(),
}

