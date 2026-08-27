"""LLM token kullanimi + cagri gozlemlenebilirligi.

REFACTOR_PLAN.md Adim 3: 2026-08-26'da digest'in uc slotu da sessizce
basarisiz oldu cunku hiçbir cagrisi ``token_usage``'a yazmiyordu -- hata
yalniz ucucu container logundaydi. ``log_token_usage`` artik hem basarili
hem basarisiz cagrilar icin cagrilir (bkz. ``src/llm/agents.py::log_llm_call``,
``src/services/digest/service.py::generate_digest``,
``src/services/report/__init__.py::generate_report``); basarisiz bir cagride
token sayilari ``NULL`` olabilir (istek hic tamamlanmadi).
"""

from datetime import datetime, timezone

from fastapi import HTTPException

from src.core.database import db

# get_token_summary'nin group_by parametresi icin allowlist -- kullanici
# girdisi (admin endpoint query param) dogrudan SQL'e (GROUP BY/SELECT
# kolonuna) ulastigi icin serbest string kabul edilmez (bkz.
# src/api/reports.py::_sort_order_clause deseni, backend/AGENTS.md).
_GROUP_BY_COLUMNS: dict[str, str] = {
    "provider": "provider",
    "model": "model",
    "purpose": "purpose",
}


async def log_token_usage(
    model: str,
    *,
    purpose: str,
    provider: str | None = None,
    status: str = "ok",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    endpoint: str | None = None,
    user_id: int | None = None,
) -> None:
    """Bir LLM cagrisini (basarili veya basarisiz) ``token_usage``'a yazar.

    ``endpoint`` gecmisten kalan kolon -- SILINMEDI (eski satirlar var), ama
    artik ``purpose`` birincil alan. ``endpoint`` acikca verilmezse ``purpose``
    ile AYNI deger yazilir: boylece eski ``endpoint=`` filtreli sorgular/
    dashboardlar (varsa) yeni satirlarda da calismaya devam eder, ayri bir
    "iki alan birbirinden sapti" bakim yuku dogmaz. ``status`` basarisiz bir
    cagrida ``"error"`` olmali; bu durumda token sayilari genelde bilinmez ve
    ``NULL`` birakilabilir (kolonlar bunun icin nullable, bkz. migrations/
    014_llm_observability.sql).

    Cagiran taraf bu fonksiyonu tipik olarak ``src.llm.agents.log_llm_call``
    uzerinden cagirir -- o sarici loglama hatasini yutup ana LLM cagrisini
    hicbir zaman dusurmez. Burada ek bir try/except YOK; DB hatasi burada
    firlatilirsa sorumluluk cagirandadir (log_llm_call bunu zaten saglar).
    """
    if endpoint is None:
        endpoint = purpose
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            """INSERT INTO token_usage
               (model, prompt_tokens, completion_tokens, total_tokens, endpoint,
                purpose, provider, status, error, duration_ms, user_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                endpoint,
                purpose,
                provider,
                status,
                error,
                duration_ms,
                user_id,
                datetime.now(timezone.utc),
            ),
        )
        await db.commit()


async def get_token_summary(
    since: datetime | None = None,
    endpoint: str | None = None,
    purpose: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    group_by: str | None = None,
) -> dict:
    """Toplam kullanim + istege bagli saglayici/model/amac kirilimi.

    ``group_by`` verilmezse eski davranis korunur (yalniz toplam). Verilirse
    ``_GROUP_BY_COLUMNS`` allowlist'inden gecer (kullanici girdisi dogrudan
    SQL'e gitmez) ve donen sozluge o kirilimin satirlarini tasiyan
    ``"breakdown"`` anahtari eklenir.
    """
    conditions = []
    params: list = []

    if since:
        conditions.append("created_at >= %s")
        params.append(since)
    if endpoint:
        conditions.append("endpoint = %s")
        params.append(endpoint)
    if purpose:
        conditions.append("purpose = %s")
        params.append(purpose)
    if provider:
        conditions.append("provider = %s")
        params.append(provider)
    if model:
        conditions.append("model = %s")
        params.append(model)
    if status:
        conditions.append("status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            f"""SELECT
                  COUNT(*) AS call_count,
                  COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                  COALESCE(SUM(completion_tokens), 0) AS total_completion,
                  COALESCE(SUM(total_tokens), 0) AS total_tokens
              FROM token_usage {where}""",
            params,
        )
        row = await cur.fetchone()

    result = {
        "call_count": row[0],
        "total_prompt_tokens": row[1],
        "total_completion_tokens": row[2],
        "total_tokens": row[3],
    }

    if group_by is None:
        return result

    group_col = _GROUP_BY_COLUMNS.get(group_by)
    if group_col is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid group_by. Allowed: {sorted(_GROUP_BY_COLUMNS)}",
        )

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            f"""SELECT
                  {group_col} AS group_value,
                  COUNT(*) AS call_count,
                  COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                  COALESCE(SUM(completion_tokens), 0) AS total_completion,
                  COALESCE(SUM(total_tokens), 0) AS total_tokens
              FROM token_usage {where}
              GROUP BY {group_col}
              ORDER BY total_tokens DESC""",
            params,
        )
        rows = await cur.fetchall()

    result["breakdown"] = [
        {
            "group_by": group_by,
            "value": r[0],
            "call_count": r[1],
            "total_prompt_tokens": r[2],
            "total_completion_tokens": r[3],
            "total_tokens": r[4],
        }
        for r in rows
    ]
    return result
