import json
import logging
import threading
from src.core.database import db

logger = logging.getLogger(__name__)


def track_event(event_type: str, user_id: int | None = None, ticker: str | None = None, details: dict | None = None):
    threading.Thread(target=_track, args=(event_type, user_id, ticker, details), daemon=True).start()


def _track(event_type: str, user_id: int | None = None, ticker: str | None = None, details: dict | None = None):
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO analytics_events (event_type, user_id, ticker, details)
                VALUES (%s, %s, %s, %s)
            """, (event_type, user_id, ticker, json.dumps(details or {})))
            db.commit()
    except Exception as e:
        logger.warning("Analytics track_event failed: %s", e)
