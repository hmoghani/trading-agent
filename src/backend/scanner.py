"""Dynamic Market Scanner & Screener (Freqtrade / Quant Screener).

Continuously screens market for:
1. RSI Intraday Dips (< 35) for mean reversion
2. Volume Surge Breakouts (> 1.5x average volume)
3. Trend Momentum Continuations (Price > 20 EMA)

Calculates technical indicators mathematically from real OHLCV historical bars
retrieved through the Robinhood MCP client with zero mock/synthetic randomness.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List
from src.backend.config import settings
from src.backend.mcp_client import mcp_client, mcp_bridge

logger = logging.getLogger(__name__)


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """Calculate Relative Strength Index (RSI) from closing prices."""
    if len(prices) <= period:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def calculate_ema(prices: List[float], period: int = 20) -> float:
    """Calculate Exponential Moving Average (EMA) from closing prices."""
    if not prices:
        return 0.0
    if len(prices) < period:
        return round(sum(prices) / len(prices), 2)
    k = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1.0 - k)
    return round(ema, 2)


class MarketScanner:
    """Automated market screener finding high-probability trade setups."""

    def __init__(self) -> None:
        self.universe = [
            "SPY", "QQQ", "NVDA", "AAPL", "TSLA",
            "MSFT", "AMZN", "META", "GOOGL", "AMD",
        ]

    async def scan_market(self) -> List[Dict[str, Any]]:
        """Run multi-factor screener across universe using real market bars."""
        results = []

        for sym in self.universe:
            quote = await mcp_client.get_quote(sym)
            price = float(quote.get("last_trade_price", 100.0))
            prev = float(quote.get("previous_close", price))
            chg_pct = round(((price - prev) / max(prev, 0.01)) * 100, 2)

            # Retrieve real OHLCV historical candlestick bars
            bars = await mcp_client.get_historicals(sym, timeframe="1M")
            closes = [float(b["close"]) for b in bars] if bars else [price]
            volumes = [int(b.get("volume", 0)) for b in bars] if bars else [1]

            # True mathematical indicator calculations
            rsi = calculate_rsi(closes, period=14)
            ema20 = calculate_ema(closes, period=20)
            ema_trend = "Bullish" if price >= ema20 else "Bearish"

            # Volume comparison: latest bar volume vs historical average
            if len(volumes) >= 5:
                avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
                vol_mult = round(volumes[-1] / max(avg_vol, 1), 2) if avg_vol > 0 else 1.0
            else:
                vol_mult = 1.0

            setup_type = None
            setup_badge = None
            rationale = None

            if rsi <= 35.0:
                setup_type = "RSI_DIP"
                setup_badge = "Oversold Dip"
                rationale = f"RSI dropped to {rsi} (oversold). Favorable mean-reversion bounce expected."
            elif vol_mult >= 1.5 and chg_pct > 0.3:
                setup_type = "VOLUME_BREAKOUT"
                setup_badge = "Volume Breakout"
                rationale = f"Trading at {vol_mult}x volume with +{chg_pct}% gain. Momentum expansion."
            elif ema_trend == "Bullish" and 40.0 <= rsi <= 65.0 and chg_pct > 0.1:
                setup_type = "TREND_CONTINUATION"
                setup_badge = "Trend Continuation"
                rationale = f"Holding above 20 EMA (${ema20:.2f}) with healthy RSI ({rsi}). Trend momentum continuing."
            else:
                setup_type = "NEUTRAL_MOMENTUM"
                setup_badge = "Consolidation"
                rationale = f"Trading within range (RSI {rsi}, EMA ${ema20:.2f}). Monitoring for breakout."

            results.append({
                "symbol": sym,
                "price": price,
                "change_percent": chg_pct,
                "rsi": rsi,
                "volume_multiplier": vol_mult,
                "ema_trend": ema_trend,
                "setup_type": setup_type,
                "setup_badge": setup_badge,
                "rationale": rationale,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            })

        return results


market_scanner = MarketScanner()
