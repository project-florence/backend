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
        "araç kullanma. Yalnızca finansal olarak etkili bulduğun bir başlığın tam "
        "metnini okumak istersen search_news ve/veya fetch_article_text kullan. "
        "Yeterli bilgiye ulaştığında HER ZAMAN nihai Digest'i üret; araç çağırmaya "
        "devam etme."
    )
    return "\n".join(parts)


async def generate_digest(slot: str = "evening") -> Digest:
    digest_cfg = get_config()["digest"]
    if slot not in digest_cfg["slot_times"]:
        raise ValueError(f"Unknown digest slot: {slot!r}. Valid: {list(digest_cfg['slot_times'])}")

    timeout_s = float(digest_cfg.get("timeout_s", 300))
    async with asyncio.timeout(timeout_s):
        snapshot = await tools.get_market_snapshot()
        news = await tools.get_news_feed()

        agent = _build_agent()
        result = await agent.run(
            prepare_context(slot, snapshot, news),
            usage_limits=UsageLimits(
                request_limit=int(digest_cfg.get("max_requests", 200)),
                tool_calls_limit=int(digest_cfg.get("max_tool_calls", 6)),
            ),
        )

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
