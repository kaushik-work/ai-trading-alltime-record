from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

def now_ist() -> datetime:
    """Current datetime in IST. Use everywhere instead of datetime.now()."""
    return datetime.now(_IST)

def today_ist() -> str:
    """Today's date string (YYYY-MM-DD) in IST."""
    return now_ist().strftime("%Y-%m-%d")
