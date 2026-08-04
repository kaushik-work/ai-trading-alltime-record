from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

def now_ist() -> datetime:
    """Current datetime in IST. Use everywhere instead of datetime.now()."""
    return datetime.now(_IST)

