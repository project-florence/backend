from src.get_bist_companies import get_bist_companies_as_dict_from_redis

def is_valid_bist_ticker(ticker: str) -> bool:
    ticker = ticker.upper()
    return ticker in get_bist_companies_as_dict_from_redis()