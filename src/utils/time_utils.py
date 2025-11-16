"""Time utilities for timezone handling"""
from datetime import datetime, timedelta
import pytz
import pandas as pd


def ensure_utc(dt) -> datetime:
    """Convert any datetime/timestamp to UTC datetime"""
    if isinstance(dt, pd.Timestamp):
        if dt.tz is None:
            dt = dt.tz_localize('UTC')
        else:
            dt = dt.tz_convert('UTC')
        return dt.to_pydatetime()
    
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=pytz.UTC)
        return dt.astimezone(pytz.UTC)
    
    parsed = pd.Timestamp(dt)
    if parsed.tz is None:
        parsed = parsed.tz_localize('UTC')
    else:
        parsed = parsed.tz_convert('UTC')
    return parsed.to_pydatetime()


def get_interval_seconds(interval: str) -> int:
    """Convert interval string to seconds"""
    unit = interval[-1].lower() if interval else 'm'
    try:
        value = int(interval[:-1]) if interval[:-1] else 1
    except ValueError:
        value = 1
    
    multipliers = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return value * multipliers.get(unit, 60)


def estimate_candles_needed(interval: str, lookback_days: int, max_limit: int = 1500) -> int:
    """Estimate number of candles needed for lookback period"""
    unit = interval[-1].lower() if interval else 'm'
    try:
        value = int(interval[:-1]) if interval[:-1] else 1
    except ValueError:
        value = 1
    
    minutes_per_candle = {
        'm': value,
        'h': value * 60,
        'd': value * 1440,
        'w': value * 10080
    }.get(unit, value)
    
    total_minutes = lookback_days * 1440
    if minutes_per_candle <= 0:
        return max_limit
    
    needed = int(total_minutes / minutes_per_candle) + 5
    return max(10, min(max_limit, needed))



