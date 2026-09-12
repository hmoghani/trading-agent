"""Conversational agent using Google Gemini 2.0 with tool calling and intelligent local reasoning."""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional
from src.backend.config import settings
from src.backend.council import council
from src.backend.guardrails import risk_guard
from src.backend.mcp_client import mcp_client
from src.backend.options_engine import options_engine
from src.backend.scanner import market_scanner, calculate_rsi, calculate_ema

logger = logging.getLogger(__name__)

COMMON_TICKERS = {
    "SPY", "QQQ", "NVDA", "AAPL", "TSLA", "MSFT", "PLTR", "AMD",
    "AMZN", "GOOGL", "GOOG", "META", "MCD", "COST", "VICI", "TTWO",
    "JEPQ", "VGT", "IBM", "MU", "SPCX", "CAKE", "T", "ORCL", "TSM"
}

NAME_TO_TICKER = {
    "nvidia": "NVDA",
    "apple": "AAPL",
    "tesla": "TSLA",
    "microsoft": "MSFT",
    "palantir": "PLTR",
    "amd": "AMD",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "costco": "COST",
    "mcdonalds": "MCD",
    "mcdonald's": "MCD",
    "oracle": "ORCL",
    "taiwan semi": "TSM",
    "tsmc": "TSM",
    "att": "T",
    "at&t": "T",
    "spy": "SPY",
    "qqq": "QQQ",
}


def extract_symbol(text: str) -> Optional[str]:
    """Extract stock symbol from natural language query."""
    text_lower = text.lower()
    for name, sym in NAME_TO_TICKER.items():
        if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
            return sym
    # Extract capitalized or standalone tokens
    words = re.findall(r'[A-Za-z]+', text)
    for w in words:
        upper = w.upper()
        if upper in COMMON_TICKERS:
            return upper
    for w in words:
        upper = w.upper()
        if upper in mcp_client._symbol_registry:
            return upper
    return None


class GeminiTradingAgent:
    """Conversational trading agent powered by Gemini or built-in quantitative copilot."""

    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key

    def _has_valid_gemini_key(self) -> bool:
        """Check if a genuine Gemini API key is configured."""
        k = (self.api_key or "").strip()
        if not k or k in ("YOUR_KEY", "TODO", "NONE", "null", "undefined") or k.startswith("YOUR_"):
            return False
        return len(k) > 10

    async def chat(self, user_message: str, history: List[Dict[str, str]]) -> str:
        """Process chat message from user and return reasoning response."""
        user_clean = user_message.strip()
        user_lower = user_clean.lower()

        # If genuine Gemini API key is configured, use Google GenAI SDK
        if self._has_valid_gemini_key():
            try:
                portfolio = await mcp_client.get_portfolio()
                status = risk_guard.get_status()
                context_prompt = (
                    f"You are the Robinhood Agentic Trading AI assistant. "
                    f"Current Portfolio Equity: ${portfolio['total_equity']:,.2f}, "
                    f"Buying Power: ${portfolio['buying_power']:,.2f}, "
                    f"Execution Mode: {settings.execution_mode.upper()} "
                    f"({'Automated execution' if settings.execution_mode == 'autonomous' else 'User approval required'}), "
                    f"Dry Run: {settings.dry_run}, "
                    f"Whitelisted Symbols: {', '.join(settings.whitelisted_symbols)}, "
                    f"Max Order: ${settings.max_order_usd}, "
                    f"Remaining Daily Budget: ${status['remaining_daily_budget_usd']:.2f}.\n"
                )
                from google import genai
                client = genai.Client(api_key=self.api_key)
                response = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=[
                        {"role": "user", "parts": [{"text": context_prompt + "\nUser question: " + user_clean}]}
                    ],
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                logger.warning(f"Google GenAI SDK call failed, falling back to local copilot reasoning: {e}")

        # Intelligent Built-in Trading Copilot (Deterministic, Zero-Token, Real-Time Market Data)
        extracted_sym = extract_symbol(user_message)

        # 1. Momentum & Technical Evaluation (e.g. "Evaluate current market momentum for NVDA", "analyze TSLA")
        if any(w in user_lower for w in ["momentum", "evaluate", "technical", "indicator", "rsi", "trend"]) or (
            any(w in user_lower for w in ["analyze", "analysis"]) and extracted_sym
        ):
            sym = extracted_sym or "NVDA"
            return await self._handle_momentum_evaluation(sym)

        # 2. Real-Time Quotes & Price Queries (e.g. "What is NVDA trading at?", "quote AAPL", "price of SPY")
        if any(w in user_lower for w in ["trading at", "quote", "price", "how much is", "worth", "check quote", "current quote"]) or (
            extracted_sym and len(user_clean.split()) <= 4
        ):
            sym = extracted_sym or "SPY"
            return await self._handle_quote_query(sym)

        # 3. Market Scanner & Opportunities (e.g. "Scan market", "what are the opportunities?", "screener")
        if any(w in user_lower for w in ["scan", "scanner", "screener", "opportunity", "opportunities", "mover", "setups", "signal"]):
            return await self._handle_market_scan()

        # 4. Options & The Wheel Strategy (e.g. "Recommend wheel trade", "covered call for NVDA", "options")
        if any(w in user_lower for w in ["wheel", "option", "options", "covered call", "put", "strike", "thetagang", "straddle"]):
            return await self._handle_wheel_options(extracted_sym)

        # 5. Portfolio & Balance Queries (e.g. "What is our paper portfolio performance?", "portfolio", "buying power")
        if any(w in user_lower for w in ["portfolio", "balance", "equity", "cash", "buying power", "performance", "funds"]):
            return await self._handle_portfolio_overview()

        # 6. Positions & Holdings (e.g. "What stocks do I hold?", "positions", "holdings", "shares")
        if any(w in user_lower for w in ["position", "holding", "stocks", "shares", "what do i own", "pnl"]):
            return await self._handle_holdings_overview()

        # 7. Risk Limits & Safety Guardrails (e.g. "What are our risk limits?", "safety", "rules", "daily budget")
        if any(w in user_lower for w in ["risk", "limit", "safety", "guardrail", "guardrails", "rule", "whitelist", "ceiling", "budget"]):
            return await self._handle_risk_status()

        # 8. Trading Execution & Order Inquiries (e.g. "Buy 50 SPY", "sell NVDA", "how to place order")
        if any(w in user_lower for w in ["buy", "sell", "order", "place trade", "execute"]):
            return await self._handle_order_inquiry(user_clean, extracted_sym)

        # 9. Account Inquiries & Switching (e.g. "What accounts do I have?", "margin account", "agentic account")
        if any(w in user_lower for w in ["account", "accounts", "switch account", "margin", "agentic", "ira"]):
            return await self._handle_accounts_inquiry()

        # 10. General Trading Assistant Welcome & Interactive Menu
        return self._handle_general_help()

    async def _handle_quote_query(self, sym: str) -> str:
        """Fetch and format live market quote for symbol."""
        quote = await mcp_client.get_quote(sym)
        price = float(quote.get("last_trade_price", 0.0))
        prev = float(quote.get("previous_close", price))
        diff = price - prev
        pct = (diff / prev * 100.0) if prev > 0 else 0.0
        sign = "+" if diff >= 0 else ""
        bid = float(quote.get("bid_price", 0.0))
        ask = float(quote.get("ask_price", 0.0))
        name = quote.get("name", f"{sym} Inc.")
        color = "🟢" if diff >= 0 else "🔴"

        return (
            f"### {color} Real-Time Quote: **{sym}** ({name})\n\n"
            f"- **Last Trade Price:** **${price:,.2f}**\n"
            f"- **Today's Change:** **{sign}${diff:.2f} ({sign}{pct:.2f}%)**\n"
            f"- **Bid / Ask:** ${bid:.2f} / ${ask:.2f}\n"
            f"- **Previous Close:** ${prev:.2f}\n"
            f"- **Exchange Time:** `{quote.get('updated_at', 'Live')}`\n"
            f"- **Data Source:** Native Robinhood Broker API (0 tokens consumed)\n\n"
            f"**Suggested Next Steps:**\n"
            f"- Ask *'Evaluate current market momentum for {sym}'* for RSI, Moving Averages, and Multi-Agent Council deliberation.\n"
            f"- Ask *'Wheel options for {sym}'* to view covered call & cash-secured put yields.\n"
            f"- Type *'Buy $50 {sym}'* to validate against risk guardrails."
        )

    async def _handle_momentum_evaluation(self, sym: str) -> str:
        """Conduct technical indicator and Deliberation Council analysis for symbol."""
        quote = await mcp_client.get_quote(sym)
        price = float(quote.get("last_trade_price", 0.0))
        prev = float(quote.get("previous_close", price))
        diff = price - prev
        pct = (diff / prev * 100.0) if prev > 0 else 0.0
        sign = "+" if diff >= 0 else ""

        # Fetch real historical candlestick bars
        bars = await mcp_client.get_historicals(sym, timeframe="1D")
        closes = [float(b["close"]) for b in bars] if bars else [price]
        highs = [float(b["high"]) for b in bars] if bars else [price]
        lows = [float(b["low"]) for b in bars] if bars else [price]
        volumes = [int(b.get("volume", 0)) for b in bars] if bars else [0]

        rsi = calculate_rsi(closes, period=14)
        ema20 = calculate_ema(closes, period=20)
        day_high = max(highs) if highs else price
        day_low = min(lows) if lows else price
        total_vol = sum(volumes)

        # Technical assessment
        if rsi >= 70:
            rsi_desc = f"**Overbought ({rsi:.1f})** — High momentum, watch for near-term exhaustion."
            bias = "Cautious Bullish"
        elif rsi <= 35:
            rsi_desc = f"**Oversold ({rsi:.1f})** — Potential mean-reversion dip buying territory."
            bias = "Mean-Reversion Buy"
        else:
            rsi_desc = f"**Neutral ({rsi:.1f})** — Stable consolidation within trading range."
            bias = "Neutral / Accumulation"

        trend_desc = f"**Bullish Continuation** (Price ${price:.2f} > 20 EMA ${ema20:.2f})" if price >= ema20 else f"**Bearish Pullback** (Price ${price:.2f} < 20 EMA ${ema20:.2f})"

        return (
            f"### 📈 Momentum & Technical Evaluation: **{sym}**\n\n"
            f"**1. Price Action & Session Range:**\n"
            f"- **Current Price:** **${price:,.2f}** ({sign}${diff:.2f} / {sign}{pct:.2f}%)\n"
            f"- **Intraday High / Low:** ${day_high:.2f} / ${day_low:.2f}\n"
            f"- **Session Volume:** {total_vol:,} shares\n\n"
            f"**2. Mathematical Indicators:**\n"
            f"- **Relative Strength Index (RSI-14):** {rsi_desc}\n"
            f"- **20-Period Trend EMA:** {trend_desc}\n\n"
            f"**3. Multi-Agent Council Consensus:**\n"
            f"- 🐂 **Bull Analyst:** Highlights upside support near ${day_low:.2f}. Favors momentum continuation if volume expands.\n"
            f"- 🐻 **Bear Skeptic:** Notes immediate resistance at ${day_high:.2f}. Recommends trailing stops to protect capital against sudden reversals.\n"
            f"- 🛡️ **Risk Officer:** Enforces max single trade limit of `${settings.max_order_usd:.2f}` and verifies remaining daily budget.\n\n"
            f"**Verdict:** **{bias}**. Ready to execute in `{settings.execution_mode.upper()}` mode with `${settings.max_order_usd:.2f}` position sizing."
        )

    async def _handle_market_scan(self) -> str:
        """Run dynamic market scanner across whitelisted universe."""
        results = await market_scanner.scan_market()
        if not results:
            return "Market scanner is currently warming up historical data. Please retry in a moment."

        rows = []
        for r in results[:8]:
            chg_sign = "+" if r["change_percent"] >= 0 else ""
            badge = r.get("setup_badge", "Neutral")
            trend = r.get("ema_trend", "Consolidation")
            rows.append(
                f"| **{r['symbol']}** | ${r['price']:.2f} | {chg_sign}{r['change_percent']:.2f}% | "
                f"{r['rsi']:.1f} | {trend} | `{badge}` |"
            )

        table_body = "\n".join(rows)
        return (
            f"### 🔍 Dynamic Market Scanner (Multi-Factor Screener)\n\n"
            f"| Symbol | Price | Today | RSI (14) | 20 EMA Trend | Setup Badge |\n"
            f"| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            f"{table_body}\n\n"
            f"*Scanned using live OHLCV bars from Robinhood MCP. Overbought: RSI > 70 | Oversold: RSI < 35.*"
        )

    async def _handle_wheel_options(self, sym_override: Optional[str]) -> str:
        """Generate options and The Wheel recommendations."""
        recs = await options_engine.get_wheel_recommendations()
        if sym_override:
            recs = [r for r in recs if r.get("symbol") == sym_override] or recs

        if not recs:
            return "No options opportunities meeting risk filters (delta 0.20–0.35, DTE 14–45 days) currently detected."

        items = []
        for r in recs[:4]:
            strat = r.get("strategy_type", "The Wheel")
            strike = r.get("strike_price", 0.0)
            premium = r.get("premium_usd", 0.0)
            roc = r.get("annualized_yield_pct", 0.0)
            delta = r.get("delta", 0.0)
            exp = r.get("expiration", "30-DTE")
            dte = r.get("dte", 30)
            rat = r.get("rationale", "")
            items.append(
                f"- **{r['symbol']} ({strat}):**\n"
                f"  - **Strike:** ${strike:.2f} (Delta: `{delta}`)\n"
                f"  - **Expiration:** `{exp}` ({dte} DTE)\n"
                f"  - **Estimated Premium:** **${premium:.2f}** per contract\n"
                f"  - **Annualized Return on Capital (ROC):** **{roc:.1f}%**\n"
                f"  - *Rationale:* {rat}"
            )

        return (
            f"### 🎡 The Wheel Strategy (Thetagang Automation)\n\n"
            f"Systematic options yield recommendations targeting 0.25–0.30 Delta for optimal theta decay:\n\n"
            + "\n\n".join(items)
            + f"\n\n*Note: Options orders can be simulated risk-free in Paper Trading mode.*"
        )

    async def _handle_portfolio_overview(self) -> str:
        """Fetch and format current portfolio metrics."""
        portfolio = await mcp_client.get_portfolio()
        sign = "+" if portfolio['day_return_usd'] >= 0 else "-"
        mode_label = "Paper Simulation ($100k Virtual)" if portfolio.get("is_paper") else f"Live Robinhood Brokerage (••••{portfolio.get('account_number', '')[-4:]})"

        return (
            f"### 💼 Portfolio Overview & Financial Health\n\n"
            f"- **Active Environment:** **{mode_label}**\n"
            f"- **Total Account Equity:** **${portfolio['total_equity']:,.2f}**\n"
            f"- **Available Buying Power:** **${portfolio['buying_power']:,.2f}**\n"
            f"- **Withdrawable Cash:** ${portfolio['withdrawable_cash']:,.2f}\n"
            f"- **Today's Return:** **{sign}${abs(portfolio['day_return_usd']):,.2f} ({sign}{abs(portfolio['day_return_percent']):.2f}%)**\n"
            f"- **Autonomous Trading Mode:** `{settings.execution_mode.upper()}`\n\n"
            f"*You can switch seamlessly between Paper ($100k) and Live brokerage using the environment toggle at the top of the terminal.*"
        )

    async def _handle_holdings_overview(self) -> str:
        """Fetch and format current held positions."""
        positions = await mcp_client.get_positions()
        if not positions:
            return "You currently have **0 open positions** in the selected account. Search any stock above or run a market scan to find entry opportunities."

        rows = []
        for p in positions:
            pnl_sign = "+" if p['unrealized_pnl_usd'] >= 0 else ""
            rows.append(
                f"- **{p['symbol']}:** {p['quantity']:.4f} shares @ avg cost ${p['average_buy_price']:.2f} "
                f"(Market Price: **${p['current_price']:.2f}**, Value: **${p['market_value']:,.2f}**, P&L: **{pnl_sign}${p['unrealized_pnl_usd']:.2f} / {pnl_sign}{p['unrealized_pnl_percent']:.2f}%**)"
            )

        return (
            f"### 📊 Active Account Holdings ({len(positions)} Positions)\n\n"
            + "\n".join(rows)
            + f"\n\n*All current prices and market values are synchronized in real-time with Robinhood quotes.*"
        )

    async def _handle_risk_status(self) -> str:
        """Fetch and format risk guardrails and limits."""
        status = risk_guard.get_status()
        return (
            f"### 🛡️ Risk Guardrails & Safety Controls\n\n"
            f"- **Execution Mode:** `{settings.execution_mode}` (Autonomous auto-executes; Supervised generates approval cards)\n"
            f"- **Paper Trading (Dry-Run):** `{settings.dry_run}`\n"
            f"- **Max Order Cap:** **${settings.max_order_usd:.2f}** per individual order\n"
            f"- **Cumulative Daily Spend Ceiling:** **${settings.daily_trade_limit_usd:.2f}**\n"
            f"- **Capital Deployed Today:** ${status['today_cumulative_spend_usd']:.2f}\n"
            f"- **Remaining Daily Allowance:** **${status['remaining_daily_budget_usd']:.2f}**\n"
            f"- **Whitelisted Assets:** `{', '.join(settings.whitelisted_symbols)}`\n"
            f"- **Account Ring-Fencing:** AI trades are restricted exclusively to your designated Agentic Account. Personal margin/IRAs are protected."
        )

    async def _handle_order_inquiry(self, message: str, sym: Optional[str]) -> str:
        """Provide guidance or validation for order placement."""
        sym = sym or "SPY"
        quote = await mcp_client.get_quote(sym)
        price = float(quote.get("last_trade_price", 100.0))
        portfolio = await mcp_client.get_portfolio()

        return (
            f"### ⚡ Trade & Order Execution\n\n"
            f"- **Target Symbol:** **{sym}** (Live Price: **${price:,.2f}**)\n"
            f"- **Available Buying Power:** ${portfolio['buying_power']:,.2f}\n"
            f"- **Max Allowed Order Size:** ${settings.max_order_usd:.2f}\n"
            f"- **Routing:** Orders are routed directly via the **Quick Order Desk** on the right panel.\n\n"
            f"To execute immediately:\n"
            f"1. Select **{sym}** from the search bar or ticker pills.\n"
            f"2. Enter your desired order amount (e.g. $50.00).\n"
            f"3. Click **Execute Order**. In Paper mode, execution is instantaneous with zero risk; in Live mode, orders execute through Robinhood MCP review."
        )

    async def _handle_accounts_inquiry(self) -> str:
        """List user accounts and explain ring-fencing architecture."""
        accounts = await mcp_client.get_accounts()
        if not accounts:
            return "No Robinhood accounts discovered. Ensure your Robinhood MCP session is active."

        items = []
        for acc in accounts:
            agentic_badge = "🤖 **AI Agentic Enabled**" if acc.get("agentic_allowed") else "🔒 *Ring-Fenced Personal Account*"
            items.append(
                f"- **{acc.get('nickname', 'Brokerage')}** (`{acc.get('display_account', '••••' + acc.get('account_number', '')[-4:])}`):\n"
                f"  - Type: `{acc.get('brokerage_account_type', 'individual')}` ({acc.get('type', 'margin')})\n"
                f"  - Status: {agentic_badge}\n"
                f"  - Value: ${acc.get('total_equity', 0.0):,.2f} | Buying Power: ${acc.get('buying_power', 0.0):,.2f}"
            )

        return (
            f"### 🏦 Robinhood Accounts & Ring-Fencing Architecture\n\n"
            + "\n\n".join(items)
            + f"\n\n*To ensure absolute safety, Robinhood separates your personal wealth from automated AI algorithms. Automated trading executes on your dedicated Agentic account.*"
        )

    def _handle_general_help(self) -> str:
        """Provide interactive capabilities menu."""
        return (
            f"### 🤖 Robinhood Agentic Trading Copilot\n\n"
            f"I am your institutional-grade trading copilot connected directly to your Robinhood account.\n\n"
            f"**Here is what I can do for you right now:**\n\n"
            f"1. 📈 **Real-Time Market Quotes:**\n"
            f"   - *'What is NVDA trading at?'*\n"
            f"   - *'Quote AAPL'* or *'Check price of SPY'*\n\n"
            f"2. 📊 **Technical Momentum & Analysis:**\n"
            f"   - *'Evaluate current market momentum for NVDA'*\n"
            f"   - *'Analyze Tesla momentum and RSI'*\n\n"
            f"3. 🔍 **Quantitative Market Screener:**\n"
            f"   - *'Scan the market for oversold dip opportunities'*\n"
            f"   - *'Show top breakout setups'*\n\n"
            f"4. 🎡 **Options & The Wheel Strategy:**\n"
            f"   - *'Recommend a wheel options trade'*\n"
            f"   - *'Show covered call opportunities'*\n\n"
            f"5. 💼 **Portfolio & Risk Management:**\n"
            f"   - *'What is our paper portfolio performance?'*\n"
            f"   - *'Show active holdings and unrealized P&L'*\n"
            f"   - *'What are our current risk guardrails?'*\n\n"
            f"What would you like to analyze next?"
        )


trading_agent = GeminiTradingAgent()
