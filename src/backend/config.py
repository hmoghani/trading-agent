"""Configuration settings for Robinhood Agentic Trading Platform."""

from typing import List, Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini AI
    gemini_api_key: str = Field(
        default="",
        description="Google Gemini API key for agent reasoning",
    )

    # Web Authentication & Master Password Gate
    dashboard_password: str = Field(
        default="tradingagent2026!",
        description="Master password required to access the dashboard (override via DASHBOARD_PASSWORD env var)",
    )
    session_secret: str = Field(
        default="rh-agentic-secret-key-change-in-production-12345",
        description="Secret key for signing session cookies",
    )
    session_expire_hours: int = Field(
        default=168,
        description="Hours before a web session cookie expires (default: 7 days)",
    )

    # Execution Mode: autonomous or supervised
    execution_mode: Literal["autonomous", "supervised"] = Field(
        default="autonomous",
        description="Autonomous executes orders directly; supervised pauses for user approval cards",
    )

    # Risk Controls
    dry_run: bool = Field(
        default=True,
        description="Whether to run in paper trading / simulation mode",
    )
    max_order_usd: float = Field(
        default=100.0,
        description="Maximum dollar amount allowed for a single order",
    )
    daily_trade_limit_usd: float = Field(
        default=500.0,
        description="Maximum cumulative daily USD amount allowed for purchases",
    )
    asset_whitelist: str = Field(
        default="SPY,QQQ,AAPL,NVDA,TSLA",
        description="Comma-separated list of symbols the agent is permitted to trade",
    )

    # Autonomous Strategy Loop
    strategy_interval_seconds: int = Field(
        default=300,
        description="Interval in seconds between autonomous market evaluation loops",
    )

    # Robinhood MCP Endpoint
    robinhood_mcp_url: str = Field(
        default="https://agent.robinhood.com/mcp/trading",
        description="Remote Robinhood MCP trading server endpoint",
    )

    # Server binding
    host: str = Field(default="0.0.0.0", description="Host address")
    port: int = Field(default=8000, description="Server port")

    @property
    def whitelisted_symbols(self) -> List[str]:
        """Return list of uppercase sanitized symbol strings from whitelist."""
        return [s.strip().upper() for s in self.asset_whitelist.split(",") if s.strip()]


settings = Settings()
