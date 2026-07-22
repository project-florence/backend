from fastapi import APIRouter, Depends, Query, HTTPException
from src.core.database import db
import src.simulation.montecarlo as montecarlo
from src.services.stats import increment_stat
from src.api.deps import get_current_user, validate_ticker
from src.core.config import get_config
from src.services.simulation_history import save_simulation, get_simulation_history, get_simulation_detail

router = APIRouter()


@router.get("/simulations/per-day-cost")
def daily_cost():
    return {"per_day_cost": get_config()["simulation"]["per_day_cost"], "round": 3}


@router.get("/simulations/estimate-cost/{ticker}")
def estimate_cost(
    ticker: str,
    days: int = Query(...),
):
    return {"cost": round(days * get_config()["simulation"]["per_day_cost"], 3)}


@router.get("/simulations/history")
def simulation_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user_id: int = Depends(get_current_user),
):
    return get_simulation_history(current_user_id, limit=limit, offset=offset)


@router.get("/simulations/history/{sim_id}")
def simulation_detail(
    sim_id: int,
    current_user_id: int = Depends(get_current_user),
):
    detail = get_simulation_detail(current_user_id, sim_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return detail


@router.get("/simulations/{ticker}")
def simulate(
    ticker: str,
    days: int = Query(...),
    bounds: str = Query("0.05"),
    target: str | None = Query(default=None),
    current_user_id: int = Depends(get_current_user),
):
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
        result = montecarlo.simulate(ticker, days, bounds, target)
    except TypeError as e:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (cost, current_user_id))
            db.commit()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (cost, current_user_id))
            db.commit()
        raise HTTPException(status_code=500, detail="Simulation failed, credits refunded.")

    increment_stat(ticker, "simulation_count")

    actual_target = str(target) if target else "auto"
    sim_id = save_simulation(
        user_id=current_user_id,
        ticker=ticker,
        days=days,
        bounds=bounds,
        target=actual_target,
        result=result,
        cost=cost,
    )

    result["simulation_id"] = sim_id
    result["ticker"] = ticker
    result["days"] = days
    result["target"] = actual_target
    result["bounds"] = bounds
    result["credits_spend"] = cost
    result["remaining_credits"] = remaining_credits
    return result
