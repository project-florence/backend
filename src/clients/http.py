"""Paylasilan async HTTP client (httpx).

Modul seviyesinde tek AsyncClient tutulur; baglanti havuzu paylasilir.
Uygulama kapanisinda ``close_client()`` cagrilir (main.py lifespan).
"""

import httpx

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
