import os
from dotenv import load_dotenv
from pydantic import BaseModel
from fredapi import Fred
from src.core.config import get_config
from src.core.redis import r
from src.core.database import db

class MacroeconomyData(BaseModel):
    usa_gdp: float
    usa_real_gdp: float
    fed_funds: float
    fed_funds_rate: float
    usa_unrate: float
    brent_crude_oil_price: float
    wti_crude_oil_price: float
    usa_consumer_cpi: float
    usa_10y_treasury: float
    dxy: float
    vix: float
    sp500: float
    nasdaq: float
    bitcoin: float


load_dotenv()

fred_api_key = os.getenv("FRED_API_KEY")
if not fred_api_key:
    raise ValueError("FRED_API_KEY not set")

fred = Fred(api_key=fred_api_key)


def _get_latest_fred_val(series_id: str) -> float:
    series = fred.get_series(series_id).dropna()
    return float(series.iloc[-1])


def _fetch_macroeconomy_data() -> MacroeconomyData:
    return MacroeconomyData(
        usa_gdp=_get_latest_fred_val("GDP"),
        usa_real_gdp=_get_latest_fred_val("GDPC1"),
        fed_funds=_get_latest_fred_val("FEDFUNDS"),
        fed_funds_rate=_get_latest_fred_val("DFF"),
        usa_unrate=_get_latest_fred_val("UNRATE"),
        brent_crude_oil_price=_get_latest_fred_val("DCOILBRENTEU"),
        wti_crude_oil_price=_get_latest_fred_val("DCOILWTICO"),
        usa_consumer_cpi=_get_latest_fred_val("CPIAUCSL"),
        usa_10y_treasury=_get_latest_fred_val("DGS10"),
        dxy=_get_latest_fred_val("DTWEXBGS"),
        vix=_get_latest_fred_val("VIXCLS"),
        sp500=_get_latest_fred_val("SP500"),
        nasdaq=_get_latest_fred_val("NASDAQCOM"),
        bitcoin=_get_latest_fred_val("CBBTCUSD"),
    )


def _cache_and_persist_macroeconomy_data(mdata: MacroeconomyData) -> None:
    r.set("MacroeconomyData", mdata.model_dump_json(), ex=get_config()["macroeconomy"]["cache_ttl"])

    with db.cursor() as cur:
        cur.execute("""
        INSERT INTO macroeconomy
            (usa_gdp, usa_real_gdp, fed_funds, fed_funds_rate, usa_unrate,
             brent_crude_oil_price, wti_crude_oil_price, usa_consumer_cpi,
             usa_10y_treasury, dxy, vix, sp500, nasdaq, bitcoin)
        VALUES (
            %(usa_gdp)s, %(usa_real_gdp)s, %(fed_funds)s, %(fed_funds_rate)s, %(usa_unrate)s,
            %(brent_crude_oil_price)s, %(wti_crude_oil_price)s, %(usa_consumer_cpi)s,
            %(usa_10y_treasury)s, %(dxy)s, %(vix)s, %(sp500)s, %(nasdaq)s, %(bitcoin)s
        );
        """, mdata.model_dump())
        db.commit()


def get_macroeconomy_data() -> MacroeconomyData:
    mdata = r.get("MacroeconomyData")
    if mdata:
        return MacroeconomyData.model_validate_json(mdata)

    mdata = _fetch_macroeconomy_data()
    _cache_and_persist_macroeconomy_data(mdata)
    return mdata