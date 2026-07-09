from datetime import datetime


def get_current_quarter_start() -> datetime:
    today = datetime.now()
    quarter = (today.month - 1) // 3 + 1

    if quarter == 1:
        month = 1
    elif quarter == 2:
        month = 4
    elif quarter == 3:
        month = 7
    else:
        month = 10

    return datetime(today.year, month, 1)


def get_last_quarter_start() -> datetime:
    start = get_current_quarter_start()
    year = start.year
    month = start.month - 3
    if month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 1)
