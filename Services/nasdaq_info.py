from datetime import datetime, time, timedelta
import pytz

# NASDAQ runs on US/Eastern time
EASTERN = pytz.timezone("US/Eastern")

# Regular trading hours: 9:30 AM – 4:00 PM ET, Monday–Friday
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_market_open(now: datetime = None) -> bool:
    """
    Check if NASDAQ is currently open.
    """
    if now is None:
        now = datetime.now(EASTERN)
    else:
        now = now.astimezone(EASTERN)

    # Closed on weekends
    if now.weekday() >= 5:
        return False

    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def time_until_close_or_open(now: datetime = None) -> timedelta:
    """
    Return timedelta until next close (if market is open)
    or next open (if market is closed).
    """
    if now is None:
        now = datetime.now(EASTERN)
    else:
        now = now.astimezone(EASTERN)

    today_open = datetime.combine(now.date(), MARKET_OPEN, tzinfo=EASTERN)
    today_close = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=EASTERN)

    if is_market_open(now):
        # Market open → time until today’s close
        return today_close - now
    else:
        # Market closed → figure out next open
        if now < today_open and now.weekday() < 5:
            return today_open - now
        else:
            # Move to next weekday
            next_day = now + timedelta(days=1)
            while next_day.weekday() >= 5:  # skip Sat/Sun
                next_day += timedelta(days=1)
            next_open = datetime.combine(next_day.date(), MARKET_OPEN, tzinfo=EASTERN)
            return next_open - now

def rth_proximity_factor(now: datetime = None) -> int:
    """
    Returns:
    - 10 if time to regular trading hours (09:30 ET) is MORE than 10 minutes
    - 1  if we are within 10 minutes of RTH
    """
    if now is None:
        now = datetime.now(EASTERN)
    else:
        now = now.astimezone(EASTERN)

    # If market is already open, we are effectively at RTH
    if is_market_open(now):
        return 1

    # Calculate time until next market open
    delta = time_until_close_or_open(now)

    return 1 if delta <= timedelta(minutes=10) else 10



def market_status_string(now: datetime = None) -> str:
    """
    Return a user-friendly status string:
    - "Market is open, time to close in HH:MM:SS"
    - "Market is closed, time to open in HH:MM:SS"
    """
    if now is None:
        now = datetime.now(EASTERN)
    else:
        now = now.astimezone(EASTERN)

    delta = time_until_close_or_open(now)
    h, remainder = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(remainder, 60)
    time_str = f"{h:02d}:{m:02d}:{s:02d}"

    if is_market_open(now):
        return f"Market is open, time to close in {time_str}"
    else:
        return f"Market is closed, time to open in {time_str}"


def is_market_closed_or_pre_market(now: datetime = None) -> bool:
    """
    Check if NASDAQ is currently closed or in pre-market (before 9:30 AM ET).
    This includes weekends and after-hours (after 4:00 PM ET).
    Replay-aware: Uses simulated time when in replay mode.
    """
    if now is None:
        # ✅ Replay-aware: Get time from replay service if active
        try:
            from Replay.replay_service import _replay_service_instance
            if (
                _replay_service_instance is not None
                and _replay_service_instance.is_replaying
                and _replay_service_instance.time_context
            ):
                # Use simulated time from replay (real datetime object, not MagicMock)
                now = _replay_service_instance.time_context.get_simulated_time()
            else:
                # Normal real-time mode - use actual system time
                now = datetime.now(EASTERN)
        except (ImportError, AttributeError):
            # Fallback to normal mode if replay not available
            now = datetime.now(EASTERN)
    else:
        now = now.astimezone(EASTERN)

    # Check for weekend closure
    if now.weekday() >= 5: # Saturday or Sunday
        return True
    
    # Check for time closure
    if now.time() < MARKET_OPEN or now.time() >= MARKET_CLOSE:
        return True

    return False