import json
from fastapi import APIRouter, Depends, Query, HTTPException
from src.core.config import get_config
from src.core.database import db
from src.services.report import generate_quick_report, generate_deep_report
from src.services.scout import scout_best_tickers
from src.api.deps import get_current_user, validate_ticker
from datetime import datetime
from pydantic import BaseModel

router = APIRouter()


@router.post("/reports/generate")
def generate_report(ticker: str, type: str = Query(...), current_user_id: int = Depends(get_current_user)):
    validate_ticker(ticker)

    if type not in ("quick_report", "deep_report"):
        raise HTTPException(status_code=400, detail="Invalid type")

    cost = get_config()["report"]["quick_report_cost"] if type == "quick_report" else get_config()["report"][
        "deep_report_cost"]

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
            if type == "quick_report":
                report_text = generate_quick_report(ticker)
            elif type == "deep_report":
                report_text = generate_deep_report(ticker)

        except Exception as e:
            with db.cursor() as refund_cur:
                refund_cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (cost, current_user_id))
                db.commit()
            raise HTTPException(status_code=500, detail="Report generation failed, credits refunded.")

        try:
            cur.execute("""
                        INSERT INTO reports (user_id, ticker, type, content)
                        VALUES (%s, %s, %s, %s) RETURNING id, created_at
                        """, (current_user_id, ticker, type, report_text))

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
        "report": report_text,
        "created_at": created_at
    }


@router.get("/reports/info")
def report_info():
    return {
        "quick_report": {
            "type": "quick_report",
            "name_en": "Quick Report",
            "name_tr": "Hızlı Rapor",
            "description": "Analyzes a stock based on recent news and market data, providing a concise summary of key insights, sentiment, and price action in seconds.",
            "description_tr": "Bir hisse senedi hakkında son haberler ve piyasa verileri ışığında hızlı bir analiz yapar; önemli gelişmeleri, piyasa duyarlılığını ve fiyat hareketlerini kısa ve öz bir şekilde özetler.",
            "cost": get_config()["report"]["quick_report_cost"],

        },
        "deep_report": {
            "type": "deep_report",
            "name_en": "Deep Report",
            "name_tr": "Derin Rapor",
            "description": "Performs an in-depth research on a stock by scanning a large volume of news, financial statements, and market indicators to produce a comprehensive investment analysis.",
            "description_tr": "Bir hisse senedi hakkında geniş bir haber ve veri taraması yaparak finansalları, piyasa göstergelerini ve haber akışını derinlemesine analiz eder; kapsamlı bir yatırım değerlendirmesi sunar.",
            "cost": get_config()["report"]["deep_report_cost"],
        }
    }

# Frontend'in ne alacağını netleştirmek için response model tanımlayalım
class ReportHistoryItem(BaseModel):
    id: int
    ticker: str
    type: str
    created_at: str


@router.get("/reports/history", response_model=list[ReportHistoryItem])
def get_report_history(current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                        SELECT id, ticker, type, created_at
                        FROM reports
                        WHERE user_id = %s
                        ORDER BY created_at DESC
                        """, (current_user_id,))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    history = [
        ReportHistoryItem(
            id=row[0],
            ticker=row[1],
            type=row[2],
            created_at=row[3].isoformat()
        ) for row in rows
    ]

    return history


@router.get("/reports/{report_id}")
def get_single_report(report_id: int, current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                        SELECT ticker, type, content, created_at
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

    return {
        "id": report_id,
        "ticker": row[0],
        "type": row[1],
        "content": row[2],
        "created_at": row[3].isoformat()
    }

@router.get("/scout/best-tickers")
def best_tickers(
    investment_budget: int = Query(...),
    investment_horizon: str = Query(...),
    risk_tolerance: str = Query(...),
):
    return json.loads(scout_best_tickers())
