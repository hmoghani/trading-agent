"""Market hours intelligence and session detection for US equities (ET timezone)."""

from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

EASTERN_TZ = ZoneInfo("America/New_York")

# US Stock Market Regular Trading Hours: 9:30 AM – 4:00 PM Eastern Time
RTH_OPEN = time(9, 30, 0)
RTH_CLOSE = time(16, 0, 0)

# Pre-market: 7:00 AM – 9:30 AM ET
PRE_MARKET_OPEN = time(7, 0, 0)

# After-hours: 4:00 PM – 8:00 PM ET
POST_MARKET_CLOSE = time(20, 0, 0)


def get_market_time() -> datetime:
    """Return current datetime in US Eastern timezone."""
    return datetime.now(EASTERN_TZ)


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """Check if time is within Regular Trading Hours (RTH).
    
    Monday through Friday between 9:30 AM and 4:00 PM Eastern Time.
    """
    now_et = dt or get_market_time()
    # Monday is 0, Sunday is 6
    if now_et.weekday() >= 5:
        return False
    current_time = now_et.time()
    return RTH_OPEN <= current_time < RTH_CLOSE


def get_market_status(dt: Optional[datetime] = None) -> Dict[str, Any]:
    """Return detailed market session status and countdown to next session."""
    now_et = dt or get_market_time()
    weekday = now_et.weekday()
    current_time = now_et.time()

    is_open = False
    session = "closed"
    status_text = "Market Closed"

    if weekday < 5:  # Monday to Friday
        if RTH_OPEN <= current_time < RTH_CLOSE:
            is_open = True
            session = "regular_hours"
            status_text = "Regular Market Hours (9:30 AM - 4:00 PM ET)"
        elif PRE_MARKET_OPEN <= current_time < RTH_OPEN:
            session = "pre_market"
            status_text = "Pre-Market (Closed for Regular Hours)"
        elif RTH_CLOSE <= current_time < POST_MARKET_CLOSE:
            session = "after_hours"
            status_text = "After-Hours (Closed for Regular Hours)"
        else:
            session = "closed"
            status_text = "Overnight Closed"
    else:
        session = "weekend"
        status_text = "Weekend Closed"

    # Calculate next regular market open (9:30 AM ET)
    if weekday < 4:  # Monday to Thursday
        if current_time < RTH_OPEN:
            next_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        else:
            next_open = (now_et + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    elif weekday == 4:  # Friday
        if current_time < RTH_OPEN:
            next_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        else:
            # Next Monday
            next_open = (now_et + timedelta(days=3)).replace(hour=9, minute=30, second=0, microsecond=0)
    elif weekday == 5:  # Saturday
        next_open = (now_et + timedelta(days=2)).replace(hour=9, minute=30, second=0, microsecond=0)
    else:  # Sunday
        next_open = (now_et + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)

    return {
        "is_open": is_open,
        "session": session,
        "status_text": status_text,
        "current_time_et": now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "next_open_et": next_open.strftime("%A, %b %d at %I:%M %p ET"),
        "allow_after_hours": False,
    }
