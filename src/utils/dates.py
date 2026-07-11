from datetime import datetime, timedelta
import calendar


def get_last_quarter_start() -> datetime:
    now = datetime.now()
    current_quarter = (now.month - 1) // 3
    months_back = current_quarter * 3 + 3
    start_of_quarter = now.replace(month=1, day=1) - timedelta(days=1)
    for _ in range(months_back):
        start_of_quarter = start_of_quarter.replace(day=1) - timedelta(days=1)
    return start_of_quarter.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
