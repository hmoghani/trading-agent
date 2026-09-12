"""Entrypoint runner for Robinhood Agentic Trading Platform."""

import uvicorn
from src.backend.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.backend.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
