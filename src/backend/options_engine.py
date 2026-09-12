"""Options Engine & The Wheel Strategy (Thetagang Automation).

Implements:
1. Option Chain parsing (Calls, Puts, Greeks: Delta, Theta, IV)
2. Covered Call recommendations (Selling OTM calls against held shares for yield)
3. Cash-Secured Put recommendations (Selling OTM puts to enter stocks at a discount)
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Dict, List, Optional
from src.backend.config import settings
from src.backend.mcp_client import mcp_client, mcp_bridge

logger = logging.getLogger(__name__)


class OptionsEngine:
    """Manages options chains, Greeks analysis, and automated Wheel strategy execution."""

    def __init__(self) -> None:
        self.simulated_option_orders: List[Dict[str, Any]] = []

    async def get_option_chain(self, symbol: str) -> Dict[str, Any]:
        """Fetch or generate option chain with strikes, Greeks, and premiums."""
        symbol = symbol.strip().upper()
        quote = await mcp_client.get_quote(symbol)
        curr_price = float(quote.get("last_trade_price", 100.0))

        # Check if Robinhood MCP provides real option expirations
        expirations: List[str] = []
        if mcp_bridge.is_authenticated():
            try:
                mcp_chain = await mcp_bridge.get_option_chains(symbol)
                if mcp_chain and "chains" in mcp_chain and len(mcp_chain["chains"]) > 0:
                    expirations = mcp_chain["chains"][0].get("expiration_dates", [])[:6]
            except Exception as e:
                logger.warning(f"Could not load live option chains for {symbol}: {e}")

        if not expirations:
            today = datetime.now(timezone.utc).date()
            exp_7d = (today + timedelta(days=7)).isoformat()
            exp_30d = (today + timedelta(days=30)).isoformat()
            exp_45d = (today + timedelta(days=45)).isoformat()
            expirations = [exp_7d, exp_30d, exp_45d]

        selected_exp = expirations[1] if len(expirations) > 1 else expirations[0]

        # Generate strikes around current price (steps of 5 or 2.5)
        step = 5.0 if curr_price >= 100 else 2.5
        center_strike = round(curr_price / step) * step
        strikes_data = []

        for i in range(-5, 6):
            strike = round(center_strike + (i * step), 2)
            moneyness = (curr_price - strike) / curr_price

            # Call estimates
            call_otm = strike > curr_price
            call_delta = round(max(0.05, min(0.95, 0.50 - (moneyness * 3.5))), 2)
            call_premium = round(max(0.20, (curr_price - strike) + (curr_price * 0.035 * (30 / 365) ** 0.5)), 2)
            if call_otm:
                call_premium = round(max(0.35, (curr_price * 0.04) * (1.0 - abs(moneyness) * 4)), 2)

            # Put estimates
            put_otm = strike < curr_price
            put_delta = round(max(-0.95, min(-0.05, -0.50 + (moneyness * 3.5))), 2)
            put_premium = round(max(0.20, (strike - curr_price) + (curr_price * 0.035 * (30 / 365) ** 0.5)), 2)
            if put_otm:
                put_premium = round(max(0.35, (curr_price * 0.04) * (1.0 - abs(moneyness) * 4)), 2)

            strikes_data.append({
                "strike_price": strike,
                "call": {
                    "contract_type": "call",
                    "strike_price": strike,
                    "bid": round(call_premium * 0.96, 2),
                    "ask": round(call_premium * 1.04, 2),
                    "last_trade_price": call_premium,
                    "delta": call_delta,
                    "theta": -0.06,
                    "implied_volatility": 0.32,
                    "volume": 1250,
                    "open_interest": 4820,
                },
                "put": {
                    "contract_type": "put",
                    "strike_price": strike,
                    "bid": round(put_premium * 0.96, 2),
                    "ask": round(put_premium * 1.04, 2),
                    "last_trade_price": put_premium,
                    "delta": put_delta,
                    "theta": -0.06,
                    "implied_volatility": 0.34,
                    "volume": 980,
                    "open_interest": 3950,
                },
            })

        return {
            "symbol": symbol,
            "underlying_price": curr_price,
            "expirations": expirations,
            "selected_expiration": selected_exp,
            "strikes": strikes_data,
        }

    async def get_wheel_recommendations(self) -> List[Dict[str, Any]]:
        """Generate automated Wheel Strategy trades (Covered Calls & Cash-Secured Puts)."""
        positions = await mcp_client.get_positions()
        portfolio = await mcp_client.get_portfolio()
        buying_power = float(portfolio.get("buying_power", 5000.0))

        recommendations = []

        # 1. Covered Call Scans for existing holdings
        for pos in positions:
            symbol = pos["symbol"]
            curr_price = float(pos.get("current_price", 100.0))
            chain = await self.get_option_chain(symbol)

            # Find optimal Covered Call: 30-45 DTE, Strike ~ 3% to 7% OTM, Delta between 0.20 and 0.35
            target_strike = None
            target_call = None
            for row in chain["strikes"]:
                call = row["call"]
                if 0.18 <= call["delta"] <= 0.35 and row["strike_price"] > curr_price:
                    target_strike = row["strike_price"]
                    target_call = call
                    break

            if target_call and target_strike:
                premium_per_contract = target_call["last_trade_price"] * 100
                annualized_yield = round((premium_per_contract / (target_strike * 100)) * (365 / 30) * 100, 1)
                recommendations.append({
                    "strategy_type": "Covered Call (Yield Harvest)",
                    "symbol": symbol,
                    "action": "sell_to_open",
                    "contract_type": "call",
                    "strike_price": target_strike,
                    "expiration": chain["selected_expiration"],
                    "dte": 30,
                    "delta": target_call["delta"],
                    "theta": target_call["theta"],
                    "premium_usd": premium_per_contract,
                    "annualized_yield_pct": annualized_yield,
                    "rationale": (
                        f"Sell 30-DTE ${target_strike:.2f} Call on {symbol} (Delta {target_call['delta']}). "
                        f"Generates ${premium_per_contract:.2f} premium ({annualized_yield}% annualized yield) while capping upside profit at ${target_strike:.2f}."
                    ),
                })

        # 2. Cash-Secured Put Scans on top watchlist tickers (NVDA, AAPL, SPY)
        target_put_tickers = [s for s in settings.whitelisted_symbols if s in ("NVDA", "AAPL", "SPY")]
        if not target_put_tickers and settings.whitelisted_symbols:
            target_put_tickers = settings.whitelisted_symbols[:2]

        for symbol in target_put_tickers:
            chain = await self.get_option_chain(symbol)
            curr_price = chain["underlying_price"]

            # Find optimal Cash-Secured Put: 30 DTE, Strike ~ 4% to 8% OTM, Delta between -0.20 and -0.35
            target_strike = None
            target_put = None
            for row in reversed(chain["strikes"]):
                put = row["put"]
                if -0.35 <= put["delta"] <= -0.18 and row["strike_price"] < curr_price:
                    target_strike = row["strike_price"]
                    target_put = put
                    break

            if target_put and target_strike:
                collateral_needed = target_strike * 100
                premium_per_contract = target_put["last_trade_price"] * 100
                annualized_yield = round((premium_per_contract / collateral_needed) * (365 / 30) * 100, 1)
                recommendations.append({
                    "strategy_type": "Cash-Secured Put (Discount Entry)",
                    "symbol": symbol,
                    "action": "sell_to_open",
                    "contract_type": "put",
                    "strike_price": target_strike,
                    "expiration": chain["selected_expiration"],
                    "dte": 30,
                    "delta": target_put["delta"],
                    "theta": target_put["theta"],
                    "premium_usd": premium_per_contract,
                    "collateral_usd": collateral_needed,
                    "annualized_yield_pct": annualized_yield,
                    "rationale": (
                        f"Sell 30-DTE ${target_strike:.2f} Put on {symbol} (Delta {target_put['delta']}). "
                        f"Collects ${premium_per_contract:.2f} upfront. Either expires worthless for {annualized_yield}% yield, or buys {symbol} at a {((curr_price - target_strike)/curr_price)*100:.1f}% discount."
                    ),
                })

        return recommendations

    async def get_directional_contract(self, symbol: str, side: str = "bullish") -> Optional[Dict[str, Any]]:
        """Find optimal directional options contract (60-70 Delta Calls/Puts, 7-14 DTE)."""
        chain = await self.get_option_chain(symbol)
        curr_price = chain["underlying_price"]
        is_call = side.lower() in ("bullish", "call", "buy")

        best_row = None
        best_diff = 999.0

        for row in chain["strikes"]:
            if is_call:
                contract = row["call"]
                diff = abs(contract["delta"] - 0.65)
            else:
                contract = row["put"]
                diff = abs(abs(contract["delta"]) - 0.65)

            if diff < best_diff:
                best_diff = diff
                best_row = (row, contract)

        if not best_row:
            return None

        row, contract = best_row
        premium = contract["last_trade_price"] * 100.0
        return {
            "symbol": symbol,
            "contract_type": "call" if is_call else "put",
            "strike_price": row["strike_price"],
            "underlying_price": curr_price,
            "expiration": chain["selected_expiration"],
            "delta": contract["delta"],
            "theta": contract["theta"],
            "premium_per_share": contract["last_trade_price"],
            "premium_usd": round(premium, 2),
            "target_stop_loss_pct": 20.0,
            "target_take_profit_pct": 50.0,
        }

    async def get_zero_dte_contract(self, symbol: str = "SPY", side: str = "call") -> Optional[Dict[str, Any]]:
        """Find 0DTE / 1DTE At-The-Money (ATM) contract for fast intraday scalping."""
        chain = await self.get_option_chain(symbol)
        curr_price = chain["underlying_price"]
        is_call = side.lower() in ("bullish", "call", "buy")

        # Select strike closest to current underlying price (ATM)
        best_row = None
        min_dist = 999.0

        for row in chain["strikes"]:
            dist = abs(row["strike_price"] - curr_price)
            if dist < min_dist:
                min_dist = dist
                best_row = row

        if not best_row:
            return None

        contract = best_row["call"] if is_call else best_row["put"]
        premium = contract["last_trade_price"] * 100.0
        today_str = datetime.now(timezone.utc).date().isoformat()

        return {
            "symbol": symbol,
            "contract_type": "call" if is_call else "put",
            "strike_price": best_row["strike_price"],
            "underlying_price": curr_price,
            "expiration": today_str,
            "is_zero_dte": True,
            "delta": contract["delta"],
            "theta": contract["theta"],
            "premium_per_share": contract["last_trade_price"],
            "premium_usd": round(premium, 2),
            "max_risk_cap_usd": 50.0,
            "profit_target_pct": 40.0,
        }


options_engine = OptionsEngine()

