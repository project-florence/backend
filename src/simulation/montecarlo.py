from src.services.price import get_price_history
import numpy as np
import pandas as pd
from src.services.price import get_current_price

financial_days: int = 252
sim_times: int = 10000


def _calculate_options(prices: pd.Series, days: int):
    # 1. Başlangıç fiyatını al (Son günün kapanışı)
    S0 = float(prices.iloc[-1])

    # 2. Log returnleri hesapla
    log_returns = np.log(prices / prices.shift(1))
    log_returns = log_returns.dropna()

    # 3. Drift ve Volatiliteyi düzelt
    drift = log_returns.mean()  # Günlük ortalama getiri
    volatility = log_returns.std()  # Günlük volatilite

    # 4. Z sayılarını üret (10.000 senaryo x 370 gün = 3.7 milyon sayı)
    z = np.random.standard_normal((sim_times, days))

    # 5. Günlük getirileri hesapla (Exp fonksiyonu)
    daily_returns = np.exp(drift - (volatility ** 2 / 2) + volatility * z)

    # 6. Fiyat yollarını hesapla (Vektörize edilmiş, for döngüsüne gerek yok)
    # Başlangıç fiyatını (S0) ilk gün tüm senaryolara çarp, sonra kümülatif çarp
    price_paths = np.cumprod(daily_returns, axis=1) * S0

    # 7. Bize sadece 370 gün sonreki SON FİYATLAR lazım
    options = price_paths[:, -1].tolist()

    return options


def _montecarlo(ticker: str, days: int):
    history = get_price_history(ticker, "2y", "1d")
    price_data = []
    for row in history:
        # int yerine float yapıyoruz ki kuruşlar kaybolmasın
        price_data.append(float(row['close']))

    prices = pd.Series(price_data)
    # 0 ile doldurmak yerine ffill (önceki fiyatla doldur) daha mantıklı
    prices = prices.ffill()

    options = _calculate_options(prices, days)
    return options


def probability(ticker: str, days: int, target: str | float, options = None) -> float:
    if options is None:
        options = _montecarlo(ticker, days)
    target = float(target)

    success: int = 0
    total = len(options)
    for option in options:
        if option >= target:
            success += 1

    return float(success / total)


def confidence_interval(ticker: str, days: int, bounds: str | float, options = None):
    if options is None:
        options = _montecarlo(ticker, days)
    options.sort()
    bounds = float(bounds)

    lower_bound = int(len(options) * bounds)
    upper_bound = int(len(options) * (1 - bounds))

    min_price = options[lower_bound]
    max_price = options[upper_bound]

    # min ve max diye değişken atamak Python'da gömülü fonksiyonları ezar, o yüzden min_price yaptım
    return {"min": min_price, "max": max_price, "percent": 1.0 - 2 * bounds, "days": days, "bounds": str(bounds)}

def simulate(ticker: str, days: int, bounds : str | float = "0.05", target: str | None = None):
    options = _montecarlo(ticker, days)
    if target is None:
        target = get_current_price(ticker)
        if target is None:
            raise TypeError("target cannot be None")
        target = target + ((target * 10) / 100)

    probability_output = probability(ticker, days, target, options)
    confidence_output = confidence_interval(ticker, days, bounds, options)
    return {"probability": probability_output, "confidence": confidence_output}