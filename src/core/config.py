"""Florence yapilandirmasi: .env + varsayilanlar (config.toml kaldirildi).

Dis arayuz AYNEN korunur: ``get_config()`` / ``reload_config()`` / ``init_config()``.
Tum degerler ortam degiskenlerinden ve varsayilanlar dict'inden okunur.

Env semasi: ``<SECTION>_<KEY>`` (ornek: ``report.token_cost_per_1k`` ->
``REPORT_TOKEN_COST_PER_1K``). Mevcut anahtarlarla uyumludur (``NEWS_SEARCH_URL``
-> ``news_search.search_url``, ``EMBEDDING_BASE_URL`` -> ``embedding.base_url``).

Eski ``config.toml`` dosyasi varsa uyari loglanir ama OKUNMAZ.
"""

import copy
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Varsayilan yapilandirma (eski config.toml degerlerinin birebir karsiligi).
_DEFAULTS: dict = {
    "company_info": {
        "cache_ttl": 86400,
        "request_delay_min": 0.5,
        "request_delay_max": 1.5,
        "batch_size": 10,
        "batch_delay": 5,
        "max_retries": 3,
        "retry_sleep_min": 1,
        "retry_sleep_max": 3,
    },
    "article_analyzer": {
        "quick_report_article_limit": 10,
        "deep_report_article_limit": 100,
    },
    "gdelt_api": {
        "max_records": 250,
        "base_url": "https://api.gdeltproject.org/api/v2/doc/doc",
        "mode": "ArtList",
        "format": "json",
        "timeout": 30,
    },
    "article_collector": {
        "bigquery_project": "project-florence-1",
        "default_limit": 100,
        "gdelt_gqg_table": "gdelt-bq.gdeltv2.gqg",
        "gdelt_gkg_table": "gdelt-bq.gdeltv2.gkg_partitioned",
        "cache_ttl": 3600,
    },
    "get_bist_companies": {
        "cache_interval": 2592000,
    },
    "llm_client": {
        "type": "custom",
        "openrouter_url": "https://openrouter.ai/api/v1",
        "custom_url": "http://localhost:7777/v1",
        "custom_model": "gemma",
    },
    "generate_bist_mapping": {
        "scrape_url": "https://borsacoo.com/firmalar",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "timeout": 30,
        "output_dir": "data",
        "output_filename": "bist_companies.json",
    },
    "price_history": {
        "rate_limit_delay": 1.5,
        "cache_ttl": 60,
        "cache_ttl_hot": 604800,
        "cache_ttl_intraday": 30,
        "stale_ttl": 30,
    },
    "economy": {
        "api_url": "https://api.genelpara.com/json/",
        "cache_ttl": 1200,
    },
    "halkarz": {
        "base_url": "https://halkarz.com",
        "wp_api": "https://halkarz.com/wp-json/wp/v2/posts",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "list_cache_ttl": 3600,
        "detail_cache_ttl": 3600,
    },
    "embedding": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "model": "mxbai-embed-large",
    },
    "report": {
        "token_cost_per_1k": 0.05,
        "quick_report_max_tokens": 5000,
        "deep_report_max_tokens": 50000,
    },
    "simulation": {
        "per_day_cost": 0.005,
    },
    "stock_vector": {
        "ttl": 86400,
        "risk_tolerance_map": {"low": 0.10, "medium": 0.50, "high": 0.90},
        "horizon_map": {"short": 0.15, "medium": 0.40, "long": 0.70},
        "profitability_map": {"low": 0.10, "medium": 0.35, "high": 0.70},
    },
    "news_search": {
        "search_url": "http://localhost:5435/search",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "categories": "news",
    },
    "macroeconomy": {
        "cache_ttl": 86400,
    },
    "credits": {
        "default_credits": 100,
    },
}

config = None


def _coerce(env_name: str, value: str, default):
    """Ortam degiskeni degerini varsayilanin tipine cevirir (JSON dict dahil)."""
    if isinstance(default, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, dict):
        return json.loads(value)
    return value


def _build_config() -> dict:
    cfg = copy.deepcopy(_DEFAULTS)
    for section, keys in _DEFAULTS.items():
        for key, default in keys.items():
            env_name = f"{section.upper()}_{key.upper()}"
            raw = os.getenv(env_name)
            if raw is None:
                continue
            try:
                cfg[section][key] = _coerce(env_name, raw, default)
            except Exception as e:
                logger.warning(
                    "Invalid value for %s (%r), using default: %s", env_name, raw, e
                )
    return cfg


def _warn_if_legacy_toml():
    config_path = Path(__file__).parent.parent.parent / "config.toml"
    if config_path.exists():
        logger.warning(
            "config.toml at %s is no longer read; configuration now comes from "
            "environment variables (get_config()). Remove the file to silence this warning.",
            config_path,
        )


def is_production() -> bool:
    return os.getenv("ENVIRONMENT", "development") == "production"


def is_development() -> bool:
    return not is_production()


def init_config():
    global config
    config = _build_config()
    _warn_if_legacy_toml()


def get_config():
    global config
    if config is None:
        init_config()
    return config


def reload_config():
    global config
    config = None
    init_config()
