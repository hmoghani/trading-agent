"""FastAPI application for Robinhood Agentic Trading Dashboard & Autonomous Engine."""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.backend.agent import trading_agent
from src.backend.auth import (
    create_session_jwt,
    get_current_user,
    get_optional_user,
)
from src.backend.config import settings
from src.backend.council import council
from src.backend.engine import engine
from src.backend.guardrails import GuardrailViolation, risk_guard
from src.backend.mcp_client import mcp_client
from src.backend.options_engine import options_engine
from src.backend.scanner import market_scanner
from src.backend.strategies import AVAILABLE_STRATEGIES

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start autonomous trading engine on startup and stop on shutdown."""
    logger.info("Initializing Robinhood Agentic Platform...")
    engine.start()
    yield
    logger.info("Shutting down Robinhood Agentic Platform...")
    engine.stop()


app = FastAPI(
    title="Robinhood Agentic Trading Platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount frontend static directory
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


# ==========================================
# Authentication Routes (Robinhood SSO Gate)
# ==========================================

from src.backend.auth import (
    build_robinhood_sso_url,
    create_session_jwt,
    decode_session_jwt,
    exchange_code_for_token,
    generate_code_challenge,
    generate_code_verifier,
    get_cached_robinhood_token,
    get_current_user,
    get_optional_user,
    verify_dashboard_password,
)
from datetime import datetime, timezone
import secrets


class LoginRequest(BaseModel):
    password: str


@app.get("/login", response_class=HTMLResponse)
async def serve_login_page(request: Request):
    """Serve the dashboard login page."""
    if get_optional_user(request):
        return RedirectResponse(url="/", status_code=302)

    login_file = os.path.join(frontend_dir, "login.html")
    if os.path.exists(login_file):
        with open(login_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Login page missing</h1>")


@app.post("/api/auth/login")
async def login_with_password(payload: LoginRequest, response: Response):
    """Validate master dashboard password and issue authenticated session cookie."""
    if not verify_dashboard_password(payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect master password. Please verify and try again.",
        )

    user_info = {
        "username": "operator",
        "role": "admin",
        "authenticated_at": datetime.now(timezone.utc).isoformat(),
    }
    session_jwt = create_session_jwt(user_info)
    response.set_cookie(
        key="session_token",
        value=session_jwt,
        httponly=True,
        max_age=settings.session_expire_hours * 3600,
        samesite="lax",
    )
    return {"status": "success", "username": "operator"}



@app.get("/api/auth/robinhood")
async def start_robinhood_sso(request: Request, response: Response):
    """Initiate official Robinhood OAuth 2.0 PKCE redirect flow."""
    # Determine base redirect URI
    host = request.headers.get("host", "localhost:8000")
    proto = "https" if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https" or (not host.startswith("localhost") and not host.startswith("127.0.0.1")) else "http"
    redirect_uri = f"{proto}://{host}/api/auth/callback"

    # Generate PKCE parameters and state
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    state = secrets.token_urlsafe(16)

    # Build Robinhood authorization URL
    auth_url = build_robinhood_sso_url(
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        state=state,
    )

    # Redirect user to Robinhood to authenticate
    redirect_resp = RedirectResponse(url=auth_url, status_code=302)

    # Store verifier and state in temporary HttpOnly cookies (10 min expiry)
    redirect_resp.set_cookie(key="oauth_verifier", value=verifier, httponly=True, max_age=600, samesite="lax")
    redirect_resp.set_cookie(key="oauth_state", value=state, httponly=True, max_age=600, samesite="lax")
    redirect_resp.set_cookie(key="oauth_redirect_uri", value=redirect_uri, httponly=True, max_age=600, samesite="lax")

    return redirect_resp


@app.get("/api/auth/callback")
async def robinhood_oauth_callback(
    request: Request,
    response: Response,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Receive authorization code from Robinhood, exchange for tokens, and set session cookie."""
    if error:
        logger.error(f"Robinhood OAuth error: {error}")
        return RedirectResponse(url=f"/login?error={error}", status_code=302)

    if not code:
        return RedirectResponse(url="/login?error=no_code_received", status_code=302)

    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        logger.warning("OAuth state mismatch in callback.")
        return RedirectResponse(url="/login?error=state_mismatch", status_code=302)

    verifier = request.cookies.get("oauth_verifier")
    redirect_uri = request.cookies.get("oauth_redirect_uri") or "http://localhost:8000/api/auth/callback"

    if not verifier:
        return RedirectResponse(url="/login?error=missing_verifier", status_code=302)

    try:
        # Exchange code + PKCE verifier for official Robinhood tokens
        token_data = exchange_code_for_token(
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        logger.error(f"Failed to exchange OAuth code with Robinhood: {e}")
        return RedirectResponse(url=f"/login?error=token_exchange_failed", status_code=302)

    # Create user session data
    user_info = {
        "username": "robinhood_authenticated_user",
        "access_token": token_data.get("access_token"),
        "authenticated_at": datetime.now(timezone.utc).isoformat(),
    }
    session_jwt = create_session_jwt(user_info)

    # Redirect to dashboard with session cookie
    dashboard_resp = RedirectResponse(url="/", status_code=302)
    dashboard_resp.set_cookie(
        key="session_token",
        value=session_jwt,
        httponly=True,
        max_age=settings.session_expire_hours * 3600,
        samesite="lax",
    )

    # Clear temporary oauth cookies
    dashboard_resp.delete_cookie("oauth_verifier")
    dashboard_resp.delete_cookie("oauth_state")
    dashboard_resp.delete_cookie("oauth_redirect_uri")

    return dashboard_resp


@app.post("/api/auth/logout")
async def logout(response: Response) -> Dict[str, str]:
    """Log out and clear session cookie."""
    response.delete_cookie("session_token")
    return {"status": "logged_out"}


@app.get("/api/auth/me")
async def get_me(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Return currently authenticated user profile."""
    return {"authenticated": True, "user": user}


# ==========================================
# Health & Status (Kubernetes Probes - Public)
# ==========================================

@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    """Liveness & Readiness probe endpoint for Kubernetes."""
    return {
        "status": "healthy",
        "service": "robinhood-agent",
        "execution_mode": settings.execution_mode,
        "dry_run": settings.dry_run,
        "engine_running": engine.is_running,
    }


# ==========================================
# Protected Portfolio & Market Endpoints
# ==========================================

@app.get("/api/accounts")
async def get_accounts(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    """Retrieve all discovered Robinhood brokerage accounts with balances and AI permissions."""
    return await mcp_client.get_accounts()


class SelectAccountPayload(BaseModel):
    account_number: str


@app.post("/api/accounts/select")
async def select_account(payload: SelectAccountPayload, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Select active Robinhood live brokerage account."""
    mcp_client.set_active_account(payload.account_number)
    return {"status": "success", "active_account": payload.account_number}


@app.get("/api/search")
async def search_symbols(q: str = "", user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    """Search equities by ticker or company name."""
    return await mcp_client.search_symbols(q)


@app.get("/api/portfolio")
async def get_portfolio(
    mode: Optional[str] = None,
    account: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Retrieve portfolio metrics and balances for Paper or Live mode."""
    dry_run = True if mode == "paper" else (False if mode == "live" else None)
    return await mcp_client.get_portfolio(dry_run=dry_run, account_number=account)


@app.get("/api/positions")
async def get_positions(
    mode: Optional[str] = None,
    account: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Retrieve open equity positions for Paper or Live mode."""
    dry_run = True if mode == "paper" else (False if mode == "live" else None)
    return await mcp_client.get_positions(dry_run=dry_run, account_number=account)


@app.get("/api/quote/{symbol}")
async def get_quote(symbol: str, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Retrieve price quote for a symbol."""
    return await mcp_client.get_quote(symbol)


@app.get("/api/historicals/{symbol}")
async def get_historicals(
    symbol: str,
    timeframe: str = "1D",
    user: Dict[str, Any] = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Retrieve OHLCV candlestick historical data for TradingView charts."""
    return await mcp_client.get_historicals(symbol=symbol, timeframe=timeframe)


@app.get("/api/market/status")
async def get_market_status_endpoint(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Retrieve live US equity market status, session, and next open."""
    from src.backend.market_hours import get_market_status
    return get_market_status()


@app.get("/api/safety")
async def get_safety_status(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Retrieve risk limits and daily spend metrics."""
    return risk_guard.get_status()


@app.get("/api/trades")
async def get_recent_trades(
    mode: Optional[str] = None,
    limit: int = 20,
    user: Dict[str, Any] = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Retrieve executed trade history for Paper or Live mode."""
    dry_run = True if mode == "paper" else (False if mode == "live" else None)
    return mcp_client.get_trades(dry_run=dry_run, limit=limit)


@app.post("/api/paper/reset")
async def reset_paper_account(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Reset virtual paper account to $100,000 virtual cash."""
    mcp_client.reset_paper_account()
    await engine.broadcast_event("paper_reset", {"message": "Paper account reset to $100,000.00 virtual cash."})
    return {"status": "success", "message": "Paper wallet reset to $100,000.00"}


class ManualOrderPayload(BaseModel):
    symbol: str
    side: str
    amount_usd: float
    order_type: str = "market"
    rationale: Optional[str] = "Manual order via Order Desk"
    mode: Optional[str] = None


@app.post("/api/orders")
async def submit_manual_order(payload: ManualOrderPayload, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Submit an instant order from the trading terminal."""
    dry_run = True if payload.mode == "paper" else (False if payload.mode == "live" else None)
    
    # Auto-whitelist symbol if explicitly submitted by user from Order Desk
    if payload.symbol.upper() not in settings.whitelisted_symbols:
        settings.asset_whitelist = f"{settings.asset_whitelist}, {payload.symbol.upper()}"

    try:
        receipt = await mcp_client.place_order(
            symbol=payload.symbol,
            side=payload.side,
            amount_usd=payload.amount_usd,
            order_type=payload.order_type,
            rationale=payload.rationale or "Manual order via Order Desk",
            dry_run=dry_run,
        )
    except GuardrailViolation as gv:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(gv),
        )

    is_paper = dry_run if dry_run is not None else settings.dry_run
    await engine.broadcast_event("trade_executed", {
        "mode": "paper" if is_paper else "live",
        "receipt": receipt,
        "message": f"Order executed: {payload.side.upper()} {payload.symbol} (${payload.amount_usd:.2f})",
    })
    return {"status": "success", "receipt": receipt}


# ==========================================
# Deliberation Council, Options & Scanner Endpoints
# ==========================================

@app.get("/api/council/history")
async def get_council_history(limit: int = 15, user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    """Retrieve recent multi-agent deliberation debates."""
    return council.get_history(limit=limit)


@app.get("/api/options/chain/{symbol}")
async def get_options_chain(symbol: str, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Retrieve options chain with strikes, bid/ask, and Greeks for a symbol."""
    return await options_engine.get_option_chain(symbol)


@app.get("/api/options/recommendations")
async def get_options_recommendations(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    """Retrieve automated Wheel Strategy recommendations (Covered Calls & Cash-Secured Puts)."""
    return await options_engine.get_wheel_recommendations()


class OptionOrderPayload(BaseModel):
    symbol: str
    action: str
    contract_type: str
    strike_price: float
    expiration: str
    contracts: int = 1
    premium_usd: float


@app.post("/api/options/order")
async def execute_option_order(payload: OptionOrderPayload, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Simulate or place an option order."""
    from datetime import datetime, timezone
    order_receipt = {
        "order_id": f"opt-{int(datetime.now().timestamp() * 1000)}",
        "symbol": payload.symbol,
        "action": payload.action,
        "contract_type": payload.contract_type,
        "strike_price": payload.strike_price,
        "expiration": payload.expiration,
        "contracts": payload.contracts,
        "premium_usd": payload.premium_usd,
        "status": "filled" if settings.dry_run else "submitted",
        "is_dry_run": settings.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await engine.broadcast_event("option_trade_executed", order_receipt)
    return {"status": "success", "receipt": order_receipt}


@app.get("/api/market/scanner")
async def get_market_scans(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    """Retrieve live market scanner setups (RSI dips, volume breakouts, trend continuations)."""
    return await market_scanner.scan_market()


@app.post("/api/safety/reset-circuit-breaker")
async def reset_circuit_breaker(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Reset tripped circuit breaker to resume buy orders."""
    risk_guard.reset_circuit_breaker()
    await engine.broadcast_event("circuit_breaker_reset", {"message": "Circuit breaker reset by operator."})
    return {"status": "reset", "safety": risk_guard.get_status()}


# ==========================================
# Settings & Strategy Controls
# ==========================================

class UpdateSettingsPayload(BaseModel):
    execution_mode: Optional[str] = None
    dry_run: Optional[bool] = None
    max_order_usd: Optional[float] = None
    daily_trade_limit_usd: Optional[float] = None
    asset_whitelist: Optional[str] = None
    active_strategy: Optional[str] = None


@app.get("/api/strategies")
async def get_available_strategies(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """List all available trading strategies and rich metadata."""
    strategies_list = [strat.get_metadata() for strat in AVAILABLE_STRATEGIES.values()]
    return {
        "active_strategy": engine.active_strategy_name,
        "strategies": strategies_list,
    }


@app.post("/api/settings")
async def update_settings(payload: UpdateSettingsPayload, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Update runtime trading modes and risk boundaries."""
    if payload.execution_mode in ("autonomous", "supervised"):
        settings.execution_mode = payload.execution_mode  # type: ignore
    if payload.dry_run is not None:
        settings.dry_run = payload.dry_run
    if payload.max_order_usd is not None and payload.max_order_usd > 0:
        settings.max_order_usd = payload.max_order_usd
    if payload.daily_trade_limit_usd is not None and payload.daily_trade_limit_usd > 0:
        settings.daily_trade_limit_usd = payload.daily_trade_limit_usd
    if payload.asset_whitelist is not None:
        settings.asset_whitelist = payload.asset_whitelist
    if payload.active_strategy and payload.active_strategy in AVAILABLE_STRATEGIES:
        engine.active_strategy_name = payload.active_strategy

    await engine.broadcast_event("settings_updated", {
        "execution_mode": settings.execution_mode,
        "dry_run": settings.dry_run,
        "active_strategy": engine.active_strategy_name,
    })

    return {
        "status": "success",
        "settings": risk_guard.get_status(),
        "engine": engine.get_status(),
    }


@app.get("/api/engine/status")
async def get_engine_status(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Get current engine loop status."""
    return engine.get_status()


@app.post("/api/engine/start")
async def start_engine(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Start autonomous background strategy engine."""
    engine.start()
    return {"status": "started", "engine": engine.get_status()}


@app.post("/api/engine/stop")
async def stop_engine(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Stop autonomous background strategy engine."""
    engine.stop()
    return {"status": "stopped", "engine": engine.get_status()}


@app.post("/api/engine/scan")
async def trigger_scan(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Trigger an immediate on-demand market scan pass."""
    asyncio.create_task(engine.execute_cycle())
    return {"status": "scan_triggered"}


# ==========================================
# Trade Proposals (Supervised Mode)
# ==========================================

@app.get("/api/proposals")
async def get_proposals(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    """List unapproved trade proposals."""
    return engine.get_pending_proposals()


@app.post("/api/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Approve a proposed trade."""
    try:
        receipt = await engine.approve_proposal(proposal_id)
        return {"status": "approved", "receipt": receipt}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Reject a proposed trade."""
    try:
        res = await engine.reject_proposal(proposal_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==========================================
# Real-Time SSE Stream & Agent Chat
# ==========================================

@app.get("/api/stream/events")
async def stream_events(request: Request) -> StreamingResponse:
    """Server-Sent Events (SSE) stream for live strategy logs, scans, and trade alerts."""
    # Check session cookie for SSE
    if not get_optional_user(request):
        raise HTTPException(status_code=401, detail="Authentication required for event stream.")

    queue = engine.subscribe()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Send initial backlog of recent activity
            for log_entry in reversed(engine.get_recent_logs(limit=15)):
                yield f"data: {json.dumps(log_entry)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            engine.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None


@app.post("/api/chat")
async def chat_with_agent(req: ChatRequest, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Chat with the Gemini trading agent."""
    reply = await trading_agent.chat(req.message, req.history or [])
    return {"reply": reply}


# ==========================================
# Dashboard Web UI Root Route (Protected)
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    """Serve the single-page web dashboard, redirecting to /login if unauthenticated."""
    if not get_optional_user(request):
        return RedirectResponse(url="/login", status_code=302)

    index_file = os.path.join(frontend_dir, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Robinhood Agentic Trading Platform</h1><p>Frontend file not found.</p>")
