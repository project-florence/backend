from datetime import datetime, timezone
from src.core.database import db


def log_token_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    endpoint: str = "unknown",
    user_id: int | None = None,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO token_usage
               (model, prompt_tokens, completion_tokens, total_tokens, endpoint, user_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                endpoint,
                user_id,
                datetime.now(timezone.utc),
            ),
        )
        db.commit()


def get_token_summary(
    since: datetime | None = None,
    endpoint: str | None = None,
) -> dict:
    conditions = []
    params = []

    if since:
        conditions.append("created_at >= %s")
        params.append(since)
    if endpoint:
        conditions.append("endpoint = %s")
        params.append(endpoint)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    with db.cursor() as cur:
        cur.execute(
            f"""SELECT
                   COUNT(*) AS call_count,
                   COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                   COALESCE(SUM(completion_tokens), 0) AS total_completion,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
               FROM token_usage {where}""",
            params,
        )
        row = cur.fetchone()

    return {
        "call_count": row[0],
        "total_prompt_tokens": row[1],
        "total_completion_tokens": row[2],
        "total_tokens": row[3],
    }
