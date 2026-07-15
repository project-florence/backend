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
                        INSERT INTO reports (user_id, ticker, type, title, token_usage, content)
                        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at
                        """, (
                current_user_id, ticker, type,
                report_obj.title,
                json.dumps(report_obj.token_usage),
                report_obj.report,
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
    }


class ReportHistoryItem(BaseModel):
    id: int
    ticker: str
    type: str
    title: str | None = None
    token_usage: dict | None = None
    created_at: str


@router.get("/reports/history", response_model=list[ReportHistoryItem])
def get_report_history(current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                        SELECT id, ticker, type, title, token_usage, created_at
                        FROM reports
                        WHERE user_id = %s
                        ORDER BY created_at DESC
                        """, (current_user_id,))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

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


@router.get("/reports/{report_id}")
def get_single_report(report_id: int, current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                        SELECT ticker, type, title, token_usage, content, created_at
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

    tu = row[3]
    if isinstance(tu, str):
        tu = json.loads(tu) if tu else None
    return {
        "id": report_id,
        "ticker": row[0],
        "type": row[1],
        "title": row[2],
        "token_usage": tu,
        "content": row[4],
        "created_at": row[5].isoformat(),
    }


