from fastapi import APIRouter, Depends, Query, HTTPException
from src.core.database import db
import src.simulation.montecarlo as montecarlo
from src.services.stats import increment_stat
from src.api.deps import get_current_user, validate_ticker
from src.core.config import get_config

router = APIRouter()


@router.get("/simulations/probability/{ticker}")
def probability(ticker: str, days: int = Query(...), target: str = Query(...), current_user_id: int = Depends(get_current_user)):
    validate_ticker(ticker)
    cost = round(days * get_config()["simulation"]["per_day_cost"], 3)

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
        percent = montecarlo.probability(ticker, days, target)
    except Exception as e:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (cost, current_user_id))
            db.commit()
        raise HTTPException(status_code=500, detail="Simulation failed, credits refunded.")

    increment_stat(ticker, "simulation_count")
    return {"percent": percent, "ticker": ticker, "days": days, "target": target, "credits_spend": cost, "remaining_credits": remaining_credits}


@router.get("/simulations/confidence-interval/{ticker}")
def confidence_interval(ticker: str, days: int = Query(...), bounds: str = Query(...), current_user_id: int = Depends(get_current_user)):
    validate_ticker(ticker)
    cost = round(days * get_config()["simulation"]["per_day_cost"], 3)

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
        result = montecarlo.confidence_interval(ticker, days, bounds)
    except Exception as e:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (cost, current_user_id))
            db.commit()
        raise HTTPException(status_code=500, detail="Simulation failed, credits refunded.")

    increment_stat(ticker, "simulation_count")
    result["credits_spend"] = cost
    result["remaining_credits"] = remaining_credits
    return result
