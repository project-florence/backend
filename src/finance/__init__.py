"""Finance domain package: the FX & precious-metals data pipeline.

Vertical slice (design spec 3.1): canonical symbol registry, provider layer
(GenelPara / TCMB / frankfurter / yfinance), storage and the
``FinanceService`` orchestrator. Exposes the package singleton — legacy
services (economy.py, ticker.py) and the API glue to it in later phases.
"""

from src.finance.service import FinanceService

# Package singleton (design spec 3.2: "FinanceService singleton çıkışı").
finance_service = FinanceService()

__all__ = ["FinanceService", "finance_service"]