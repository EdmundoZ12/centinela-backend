from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy.orm import Session

from app.api.dependencies import CurrentUser
from app.analyst.service import analyze_margin_alert
from app.db.session import get_db
from app.models.core import AlertStatus, Role


router = APIRouter(tags=["analista"])


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: AlertStatus
    evidence: JsonValue
    root_cause: JsonValue | None
    confidence: float | None
    proposals: JsonValue | None
    updated_at: datetime


@router.post("/alertas/{id}/analizar", response_model=AnalysisResponse)
def analyze_alert(id: UUID, db: Annotated[Session, Depends(get_db)], user: CurrentUser):
    if user.role not in (Role.GERENTE, Role.ANALISTA):
        raise HTTPException(status_code=403, detail="No tienes permiso para analizar alertas.")
    return analyze_margin_alert(db, id, user.id)
