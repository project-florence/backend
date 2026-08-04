from fastapi import APIRouter, Depends, Query, HTTPException
from src.core.database import db
import src.simulation.montecarlo as montecarlo
from src.services.stats import increment_stat
from src.services.credits import spend as credit_spend, refund as credit_refund, get_total as get_credits
from src.services.maintenance import require_feature
from src.services.analytics import track_event
from src.api.deps import get_current_user, validate_ticker
from src.core.config import get_config
from src.services.simulation_history import save_simulation, get_simulation_history, get_simulation_detail
from src.services.price import get_current_price
from src.core.job_slots import require_job_slot

router = APIRouter()


@router.get("/simulations/per-day-cost")
def daily_cost(_: int = Depends(get_current_user)):
    return {"per_day_cost": get_config()["simulation"]["per_day_cost"], "round": 3}


@router.get("/simulations/estimate-cost/{ticker}")
def estimate_cost(
    ticker: str,
    days: int = Query(..., ge=1, le=370),
    _: int = Depends(get_current_user),
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
    days: int = Query(..., ge=1, le=370),
    bounds: str = Query("0.05"),
    target: str | None = Query(default=None),
    current_user_id: int = Depends(get_current_user),
    _: bool = Depends(require_feature("simulation")),
    __: None = Depends(require_job_slot("simulation", 600)),
):
    validate_ticker(ticker)
    if target is not None:
        try:
            if float(target) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid target price")
    cost = round(days * get_config()["simulation"]["per_day_cost"], 3)

    ok, remaining_credits = credit_spend(current_user_id, cost)
    if not ok:
        raise HTTPException(status_code=402, detail="insufficient credit")

    try:
        result = montecarlo.simulate(ticker, days, bounds, target)
    except TypeError as e:
        credit_refund(current_user_id, cost)
        raise HTTPException(status_code=400, detail="Invalid simulation parameters")
    except Exception as e:
        credit_refund(current_user_id, cost)
        raise HTTPException(status_code=500, detail="Simulation failed, credits refunded.")

    increment_stat(ticker, "simulation_count")

    actual_target = str(target) if target else "auto"
    current_price = get_current_price(ticker)
    if target is None or current_price is None:
        direction = "above"
    else:
        direction = "above" if float(target) >= current_price else "below"
    result["direction"] = direction
    sim_id = save_simulation(
        user_id=current_user_id,
        ticker=ticker,
        days=days,
        bounds=bounds,
        target=actual_target,
        result=result,
        cost=cost,
    )

    if sim_id is None:
        credit_refund(current_user_id, cost)
        raise HTTPException(status_code=500, detail="Simulation could not be saved")

    result["simulation_id"] = sim_id
    result["ticker"] = ticker
    result["days"] = days
    result["target"] = actual_target
    result["bounds"] = bounds
    result["credits_spend"] = cost
    result["remaining_credits"] = remaining_credits
    track_event("simulation_run", user_id=current_user_id, ticker=ticker, details={
        "days": days, "cost": cost,
    })
    return result
