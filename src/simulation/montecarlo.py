"""Monte Carlo price simulation for a single ticker.

Pure computation module: it never performs network I/O, never touches the
database, and has no dependency on ``src.services.price``. The caller is
responsible for fetching price history and the current price in the async
layer and passing them in; this module only turns that data into simulated
price paths and probability/confidence outputs.
"""

import numpy as np
import pandas as pd

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


def probability(target, options) -> float:
    """Simüle edilen final fiyatların ``target`` değerine eşit veya üstünde kalma oranı."""
    target = float(target)

    success: int = 0
    total = len(options)
    for option in options:
        if option >= target:
            success += 1

    return float(success / total)


def confidence_interval(days: int, bounds: str | float, options):
    """Simüle edilen final fiyatlarından güven aralığı bantını hesaplar."""
    options = sorted(options)
    bounds = float(bounds)

    lower_bound = int(len(options) * bounds)
    upper_bound = int(len(options) * (1 - bounds))

    min_price = options[lower_bound]
    max_price = options[upper_bound]

    # min ve max diye değişken atamak Python'da gömülü fonksiyonları ezar, o yüzden min_price yaptım
    return {"min": min_price, "max": max_price, "percent": 1.0 - 2 * bounds, "days": days, "bounds": str(bounds)}


def simulate_from_data(history_rows, days: int, bounds: str | float = "0.05", target=None, current_price=None) -> dict:
    """Fetch edilmiş geçmiş veriden saf hesaplama ile Monte Carlo simülasyonu üretir.

    ``history_rows`` her satırı ``{"close": float}`` içeren bir dict listesidir.
    ``target`` verilmezse ``current_price`` üzerinden +%10 otomatik hedef üretilir;
    ikisi de yoksa ``ValueError`` yükselir.
    """
    price_data = []
    for row in history_rows:
        # int yerine float yapıyoruz ki kuruşlar kaybolmasın
        price_data.append(float(row['close']))

    prices = pd.Series(price_data)
    # 0 ile doldurmak yerine ffill (önceki fiyatla doldur) daha mantıklı
    prices = prices.ffill()

    options = _calculate_options(prices, days)

    if target is None:
        if current_price is None:
            raise ValueError("target requires current_price")
        current = float(current_price)
        target = current + ((current * 10) / 100)

    prob_above = probability(target, options)
    prob_below = 1.0 - prob_above
    confidence_output = confidence_interval(days, bounds, options)
    return {"prob_above": prob_above, "prob_below": prob_below, "confidence": confidence_output}
