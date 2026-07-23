from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.plans import service
from app.plans.schemas import PlanOut

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db)) -> list[PlanOut]:
    """Public pricing endpoint — used by the marketing site / signup flow."""
    return service.list_effective_plans(db)
