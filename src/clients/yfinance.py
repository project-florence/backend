import yfinance as yf
import threading
import time
import random
from src.core.config import get_config

_yfinance_lock = threading.Lock()
_last_request = 0.0


def _wait_for_rate_limit():
    global _last_request
    delay = get_config()["price_history"]["rate_limit_delay"]

    with _yfinance_lock:
        now = time.time()
        elapsed = now - _last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)
        _last_request = time.time()


def fetch_company_info(ticker_symbol: str, max_retries: int | None = None) -> dict:
    cfg = get_config()["company_info"]
    if max_retries is None:
        max_retries = cfg["max_retries"]

    for attempt in range(max_retries):
        try:
            _wait_for_rate_limit()
            ticker = yf.Ticker(ticker_symbol)
            info = ticker.info
            if info:
                try:
                    recs = ticker.recommendations
                    if recs is not None and not recs.empty:
                        info["recommendations"] = recs.reset_index().to_dict(orient="records")
                except Exception:
                    pass
                return info
        except Exception as e:
            print(f"yfinance hatasi ({ticker_symbol}), deneme {attempt + 1}/{max_retries}: {e}")

        if attempt < max_retries - 1:
            time.sleep(random.uniform(cfg["retry_sleep_min"], cfg["retry_sleep_max"]))

    print(f"MAX RETRY asildi ({ticker_symbol})")
    return {}


def fetch_price_history(ticker: str, interval: str, start, end):
    _wait_for_rate_limit()
    return yf.Ticker(ticker).history(start=start, end=end, interval=interval)
