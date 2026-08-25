"""Market digest generation service.

``generate_digest`` runs a fresh agent (avoids stale tool state), normalizes
the model output and persists it to Redis with a TTL. Redis is down-tolerant:
a digest is still returned when the cache write fails.
"""

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timezone

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from src.core.config import get_config
from src.core.database import db
from src.core.redis import r
from src.services.digest import tools
from src.services.digest.agent import _build_agent
from src.services.digest.models import Digest

logger = logging.getLogger(__name__)


def prepare_context(slot: str, snapshot: dict, news: list) -> str:
    """Build the run prompt embedding the pre-collected market data."""
    parts = [
        "Bugünün (TODAY) piyasa/makro/şirket haber bültenini hazırla.",
        f"Tarih: {date.today().isoformat()}",
        f"Slot: {slot}",
    ]
    if snapshot:
        parts.append(
            "Piyasa görünümü (önceden toplandı):\n"
            + json.dumps(snapshot, ensure_ascii=False, default=str)
        )
    else:
        parts.append("Piyasa görünümü: veri alınamadı (boş).")
    if news:
        parts.append(
            "Bugünün haber başlıkları (önceden toplandı):\n"
            + json.dumps(news, ensure_ascii=False, default=str)
        )
    else:
        parts.append("Bugünün haber başlıkları: veri alınamadı (boş).")
    parts.append(
        "Tüm piyasa ve haber verisi yukarıda zaten sağlandı; veri toplamak için "
        "araç kullanma. Yalnızca finansal olarak en etkili, en yüksek içerikli "
        "başlıkları seç ve en fazla 1-3 tam metin oku (hiçbir makaleyi tekrar "
        "okuma). search_news / fetch_article_text bütçeleri sınırlıdır; tekrar "
        "tekrar çağırma. Bütçe tükendi işareti görürsen aracı bir daha kullanma. "
        "Yeterli bilgiye ulaştığında HER ZAMAN nihai Digest'i üret; araç çağırmaya "
        "devam etme."
    )
    return "\n".join(parts)


async def generate_digest(slot: str = "evening") -> Digest:
    digest_cfg = get_config()["digest"]
    if slot not in digest_cfg["slot_times"]:
        raise ValueError(f"Unknown digest slot: {slot!r}. Valid: {list(digest_cfg['slot_times'])}")

    timeout_s = float(digest_cfg.get("timeout_s", 3600))
    tools.reset_budget()
    async with asyncio.timeout(timeout_s):
        snapshot = await tools.get_market_snapshot()
        news = await tools.get_news_feed()

        agent = _build_agent()
        try:
            result = await agent.run(
                prepare_context(slot, snapshot, news),
                usage_limits=UsageLimits(request_limit=int(digest_cfg.get("max_requests", 200))),
            )
        except UsageLimitExceeded:
            usage = tools.get_budget_usage()
            max_search = int(digest_cfg.get("max_search", 10))
            max_fetch = int(digest_cfg.get("max_fetch", 20))
            exhausted = [
                name
                for name, count, budget in (
                    ("search_news", usage["search_count"], max_search),
                    ("fetch_article_text", usage["fetch_count"], max_fetch),
                )
                if count >= budget
            ]
            logger.warning(
                "Digest generation hit request_limit without producing output "
                "(slot=%s, search_calls=%d/%d, fetch_calls=%d/%d, exhausted_budgets=%s)",
                slot,
                usage["search_count"],
                max_search,
                usage["fetch_count"],
                max_fetch,
                exhausted or "none",
            )
            raise

    digest: Digest = result.output
    digest.id = uuid.uuid4().hex
    digest.date = date.today()
    digest.slot = slot  # type: ignore[assignment]
    digest.created_at = datetime.now(timezone.utc)
    digest.language = "tr"
    digest.metadata = {
        **(digest.metadata or {}),
        "slot": slot,
        "generated_at": digest.created_at.isoformat(),
    }

    try:
        payload = json.dumps(digest.model_dump(mode="json"))
        await r.set(digest_cfg["redis_key"], payload, ex=digest_cfg["redis_ttl"])
    except Exception as e:
        logger.warning("Failed to cache digest in Redis: %s", e)

    try:
        async with db.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO digests (id, date, slot, title, content, sections, metadata, language, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    digest.id,
                    digest.date,
                    digest.slot,
                    digest.title,
                    digest.content,
                    json.dumps([s.model_dump() for s in digest.sections]),
                    json.dumps(digest.metadata),
                    digest.language,
                    digest.created_at,
                ),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to persist digest in DB: %s", e)

    return digest
