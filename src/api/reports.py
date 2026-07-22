import math
import json
from fastapi import APIRouter, Depends, Query, HTTPException
from src.core.config import get_config
from src.core.database import db
from src.services.report import generate_quick_report, generate_deep_report
from src.api.deps import get_current_user, validate_ticker
from datetime import datetime
from pydantic import BaseModel

router = APIRouter()

TOKEN_COST_PER_1K = get_config()["report"]["token_cost_per_1k"]


def _compute_cost(total_tokens: int) -> int:
    return max(1, math.ceil(total_tokens / 1000 * TOKEN_COST_PER_1K))


@router.post("/reports/generate")
def generate_report(ticker: str, type: str = Query(...), current_user_id: int = Depends(get_current_user)):
    validate_ticker(ticker)

    if type not in ("quick_report", "deep_report"):
        raise HTTPException(status_code=400, detail="Invalid type")

    try:
        if type == "quick_report":
            report_obj = generate_quick_report(ticker)
        else:
            report_obj = generate_deep_report(ticker)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {e}")

    if report_obj is None:
        raise HTTPException(status_code=500, detail="Report generation returned no result")

    total_tokens = report_obj.token_usage.get("total", 0)
    cost = _compute_cost(total_tokens)

    with db.cursor() as cur:
        try:
            cur.execute("""
                        UPDATE users
                        SET credits = credits - %s
                        WHERE id = %s
                          AND credits >= %s RETURNING credits
                        """, (cost, current_user_id, cost))
            row = cur.fetchone()

            if row is None:
                db.rollback()
                raise HTTPException(status_code=402, detail="insufficient credit")

            db.commit()
            remaining_credits = row[0]
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

        try:
            cur.execute("""
                        INSERT INTO reports (user_id, ticker, type, title, token_usage, content, sentiments)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at
                        """, (
                current_user_id, ticker, type,
                report_obj.title,
                json.dumps(report_obj.token_usage),
                report_obj.report,
                json.dumps(report_obj.sentiments) if report_obj.sentiments else "[]",
            ))

            report_row = cur.fetchone()
            db.commit()

            report_id = report_row[0]
            created_at = report_row[1].isoformat()
        except Exception as e:
            db.rollback()
            report_id = None
            created_at = None

    return {
        "success": True,
        "report_id": report_id,
        "credits_spend": cost,
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
def report_info():
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
            created_at=row[5].isoformat(),
        )
        history.append(item)
    return history


@router.get("/reports/history", response_model=list[ReportHistoryItem])
def get_report_history(
    current_user_id: int = Depends(get_current_user),
    sort: str = Query("created_at", description="Sort: created_at, ticker"),
    order: str = Query("desc", description="Order: asc, desc"),
):
    valid_sorts = {"created_at", "ticker"}
    if sort not in valid_sorts:
        raise HTTPException(status_code=400, detail=f"Invalid sort. Allowed: {valid_sorts}")
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="Invalid order. Allowed: asc, desc")

    with db.cursor() as cur:
        try:
            cur.execute(f"""
                        SELECT id, ticker, type, title, token_usage, created_at
                        FROM reports
                        WHERE user_id = %s
                        ORDER BY {sort} {order}, id DESC
                        """, (current_user_id,))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    history = _parse_history_rows(rows)
    return history


@router.get("/reports/search", response_model=list[ReportHistoryItem])
def search_reports(
    q: str = Query(..., min_length=1, description="Search query in title and content"),
    current_user_id: int = Depends(get_current_user),
    sort: str = Query("created_at", description="Sort: created_at, ticker"),
    order: str = Query("desc", description="Order: asc, desc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    valid_sorts = {"created_at", "ticker"}
    if sort not in valid_sorts:
        raise HTTPException(status_code=400, detail=f"Invalid sort. Allowed: {valid_sorts}")
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="Invalid order. Allowed: asc, desc")

    pattern = f"%{q}%"
    with db.cursor() as cur:
        try:
            cur.execute(f"""
                        SELECT id, ticker, type, title, token_usage, created_at
                        FROM reports
                        WHERE user_id = %s
                          AND (title ILIKE %s OR content ILIKE %s)
                        ORDER BY {sort} {order}, id DESC
                        LIMIT %s OFFSET %s
                        """, (current_user_id, pattern, pattern, limit, offset))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return _parse_history_rows(rows)


@router.get("/reports/{report_id}")
def get_single_report(report_id: int, current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                        SELECT ticker, type, title, token_usage, content, sentiments, created_at
                        FROM reports
                        WHERE id = %s
                          AND user_id = %s
                        """, (report_id, current_user_id))
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=404,
                                    detail="Report not found or you do not have permission to view it.")

        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    token_usage = row[3]
    if isinstance(token_usage, str):
        token_usage = json.loads(token_usage) if token_usage else None

    sentiments = row[5]
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
        "report": row[4],
        "sentiments": sentiments,
        "created_at": row[6].isoformat(),
    }


