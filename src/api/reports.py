import asyncio
import json
import math

from fastapi import APIRouter, Depends, Query, HTTPException, Response
from pydantic import BaseModel

from src.api.deps import get_current_user, validate_ticker
from src.core.config import get_config
from src.core.database import db
from src.core.job_slots import require_job_slot
from src.services.analytics import track_event
from src.services.credits import spend as credit_spend, refund as credit_refund, get_total as get_credits
from src.services.maintenance import require_feature
from src.services.report import generate_report, get_report_by_id, report_to_str
from src.utils.file_utils import markdown_to_docx, markdown_to_pdf

router = APIRouter()

TOKEN_COST_PER_1K = get_config()["report"]["token_cost_per_1k"]

# ORDER BY ifadeleri icin allowlist (SQL injection kapali; gecersiz anahtar 400).
SORT_COLUMNS = {"created_at": "created_at", "ticker": "ticker"}
ORDER = {"asc": "ASC", "desc": "DESC"}


def _sort_order_clause(sort: str, order: str) -> tuple[str, str]:
    sort_col = SORT_COLUMNS.get(sort)
    if sort_col is None:
        raise HTTPException(status_code=400, detail=f"Invalid sort. Allowed: {sorted(SORT_COLUMNS)}")
    order_dir = ORDER.get(order)
    if order_dir is None:
        raise HTTPException(status_code=400, detail="Invalid order. Allowed: asc, desc")
    return sort_col, order_dir


def _compute_cost(total_tokens: int) -> int:
    return max(1, math.ceil(total_tokens / 1000 * TOKEN_COST_PER_1K))


@router.post("/reports/generate")
async def generate_report_endpoint(
    ticker: str,
    type: str = Query(...),
    purpose: str | None = Query(None, max_length=500, description="Kullanıcının rapor amacı/sorusu"),
    current_user_id: int = Depends(get_current_user),
    _: bool = Depends(require_feature("report_generate")),
    __: None = Depends(require_job_slot("report", 900)),
):
    await validate_ticker(ticker)

    if type not in ("quick_report", "deep_report"):
        raise HTTPException(status_code=400, detail="Invalid type")

    cfg = get_config()["report"]
    max_tokens = cfg["quick_report_max_tokens"] if type == "quick_report" else cfg["deep_report_max_tokens"]
    estimated_cost = _compute_cost(max_tokens)

    ok, remaining_credits = await credit_spend(current_user_id, estimated_cost)
    if not ok:
        raise HTTPException(status_code=402, detail="insufficient credit")

    mode = type.replace("_report", "")

    try:
        report_obj = await generate_report(ticker, mode, user_id=current_user_id, purpose=purpose)
    except Exception as e:
        await credit_refund(current_user_id, estimated_cost)
        raise HTTPException(status_code=500, detail="Report generation failed")

    if report_obj is None:
        await credit_refund(current_user_id, estimated_cost)
        raise HTTPException(status_code=500, detail="Report generation returned no result")

    total_tokens = report_obj.token_usage.get("total", 0)
    actual_cost = _compute_cost(total_tokens)
    refund = estimated_cost - actual_cost

    if refund > 0:
        await credit_refund(current_user_id, refund)
        remaining_credits = await get_credits(current_user_id)
    elif actual_cost > estimated_cost:
        extra_cost = actual_cost - estimated_cost
        extra_ok, remaining_credits = await credit_spend(current_user_id, extra_cost)
        if not extra_ok:
            await credit_refund(current_user_id, estimated_cost)
            raise HTTPException(status_code=500, detail="Report cost could not be charged")

    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("""
                        INSERT INTO reports (user_id, ticker, type, title, token_usage, content, sentiments, purpose)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at
                        """, (
                current_user_id, ticker, type,
                report_obj.title,
                json.dumps(report_obj.token_usage),
                report_obj.report,
                json.dumps(report_obj.sentiments) if report_obj.sentiments else "[]",
                purpose,
            ))

            report_row = await cur.fetchone()
            await db.commit()

            report_id = report_row[0]
            created_at = report_row[1].isoformat()
        except Exception:
            await db.rollback()
            await credit_refund(current_user_id, actual_cost)
            raise HTTPException(status_code=500, detail="Report could not be saved")

    if report_id:
        await track_event("report_generated", user_id=current_user_id, ticker=ticker, details={
            "report_type": type, "tokens_used": total_tokens, "cost": actual_cost,
        })

    return {
        "success": True,
        "report_id": report_id,
        "credits_spend": actual_cost,
        "remaining_credits": remaining_credits,
        "about": ticker,
        "type": type,
        "title": report_obj.title,
        "report": report_obj.report,
        "sentiments": report_obj.sentiments,
        "token_usage": report_obj.token_usage,
        "created_at": created_at,
    }


@router.get("/reports/info")
async def report_info():
    token_cost = get_config()["report"]["token_cost_per_1k"]
    return {
        "quick_report": {
            "type": "quick_report",
            "name_en": "Quick Report",
            "name_tr": "Hızlı Rapor",
            "description": "Analyzes a stock based on recent news and market data, providing a concise summary of key insights, sentiment, and price action in seconds.",
            "description_tr": "Bir hisse senedi hakkında son haberler ve piyasa verileri ışığında hızlı bir analiz yapar; önemli gelişmeleri, piyasa duyarlılığını ve fiyat hareketlerini kısa ve öz bir şekilde özetler.",
            "est_cost": _compute_cost(20000),
        },
        "deep_report": {
            "type": "deep_report",
            "name_en": "Deep Report",
            "name_tr": "Derin Rapor",
            "description": "Performs an in-depth research on a stock by scanning a large volume of news, financial statements, and market indicators to produce a comprehensive investment analysis.",
            "description_tr": "Bir hisse senedi hakkında geniş bir haber ve veri taraması yaparak finansalları, piyasa göstergelerini ve haber akışını derinlemesine analiz eder; kapsamlı bir yatırım değerlendirmesi sunar.",
            "est_cost": _compute_cost(30000),
        },
        "token_cost_per_1k": token_cost,
        "endpoints": {
            "generate": {"method": "POST", "path": "/reports/generate", "auth": True, "params": {"ticker": "Ticker code", "type": "quick_report | deep_report"}},
            "history": {"method": "GET", "path": "/reports/history", "auth": True, "params": {"sort": "created_at | ticker (default created_at)", "order": "asc | desc (default desc)"}},
            "search": {"method": "GET", "path": "/reports/search", "auth": True, "params": {"q": "Search text", "sort": "created_at | ticker (default created_at)", "order": "asc | desc (default desc)", "limit": "Max results (default 20)", "offset": "Skip N (default 0)"}},
            "detail": {"method": "GET", "path": "/reports/{id}", "auth": True},
        },
    }


class ReportHistoryItem(BaseModel):
    id: int
    ticker: str
    type: str
    title: str | None = None
    token_usage: dict | None = None
    purpose: str | None = None
    created_at: str


def _parse_history_rows(rows: list) -> list[ReportHistoryItem]:
    history = []
    for row in rows:
        tu = row[4]
        if isinstance(tu, str):
            tu = json.loads(tu) if tu else None
        item = ReportHistoryItem(
            id=row[0],
            ticker=row[1],
            type=row[2],
            title=row[3],
            token_usage=tu,
            purpose=row[5],
            created_at=row[6].isoformat(),
        )
        history.append(item)
    return history


@router.get("/reports/history", response_model=list[ReportHistoryItem])
async def get_report_history(
    current_user_id: int = Depends(get_current_user),
    sort: str = Query("created_at", description="Sort: created_at, ticker"),
    order: str = Query("desc", description="Order: asc, desc"),
):
    sort_col, order_dir = _sort_order_clause(sort, order)

    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute(f"""
                        SELECT id, ticker, type, title, token_usage, purpose, created_at
                        FROM reports
                        WHERE user_id = %s
                        ORDER BY {sort_col} {order_dir}, id DESC
                        """, (current_user_id,))
            rows = await cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    history = _parse_history_rows(rows)
    return history


@router.get("/reports/search", response_model=list[ReportHistoryItem])
async def search_reports(
    q: str = Query(..., min_length=1, description="Search query in title and content"),
    current_user_id: int = Depends(get_current_user),
    sort: str = Query("created_at", description="Sort: created_at, ticker"),
    order: str = Query("desc", description="Order: asc, desc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    sort_col, order_dir = _sort_order_clause(sort, order)

    pattern = f"%{q}%"
    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute(f"""
                        SELECT id, ticker, type, title, token_usage, purpose, created_at
                        FROM reports
                        WHERE user_id = %s
                          AND (title ILIKE %s OR content ILIKE %s)
                        ORDER BY {sort_col} {order_dir}, id DESC
                        LIMIT %s OFFSET %s
                        """, (current_user_id, pattern, pattern, limit, offset))
            rows = await cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return _parse_history_rows(rows)


@router.get("/reports/{report_id}")
async def get_single_report(report_id: int, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("""
                        SELECT ticker, type, title, token_usage, purpose, content, sentiments, created_at
                        FROM reports
                        WHERE id = %s
                          AND user_id = %s
                        """, (report_id, current_user_id))
            row = await cur.fetchone()

            if not row:
                raise HTTPException(status_code=404,
                                    detail="Report not found or you do not have permission to view it.")

        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=500, detail="Database error")

    token_usage = row[3]
    if isinstance(token_usage, str):
        token_usage = json.loads(token_usage) if token_usage else None

    sentiments = row[6]
    if isinstance(sentiments, str):
        sentiments = json.loads(sentiments) if sentiments else []
    elif sentiments is None:
        sentiments = []

    return {
        "success": True,
        "report_id": report_id,
        "about": row[0],
        "type": row[1],
        "title": row[2],
        "token_usage": token_usage,
        "purpose": row[4],
        "report": row[5],
        "sentiments": sentiments,
        "created_at": row[7].isoformat(),
    }


@router.post("/reports/download")
async def download_report(report_id: int = Query(...), ftype: str = Query(...), current_user_id: int = Depends(get_current_user)):
    report = await get_report_by_id(report_id, current_user_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    report_str = report_to_str(report)

    if ftype == "md":
        return Response(
            content=report_str,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="report_{report_id}.md"'},
        )
    elif ftype == "docx":
        docx_bytes = await asyncio.to_thread(markdown_to_docx, report_str)
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="report_{report_id}.docx"'},
        )
    elif ftype == "pdf":
        pdf_bytes = await asyncio.to_thread(markdown_to_pdf, report_str)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="report_{report_id}.pdf"'},
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid file type.")
