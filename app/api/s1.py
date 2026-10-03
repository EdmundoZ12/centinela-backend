from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.business import alert_by_id, read_list
from app.api.dependencies import CurrentUser
from app.db.session import get_db
from app.executor.schemas import ExecutionResponse, ExecutionResult
from app.executor.service import execute_alert
from app.models.core import ExecutionAction, Role
from app.schemas.core import AlertResponse
from app.strategist.service import generate_strategy

router = APIRouter(tags=["S1"])
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.post("/alertas/{id}/estrategia", response_model=AlertResponse)
def strategy(id: UUID, db: DatabaseSession, user: CurrentUser):
    if user.role not in (Role.GERENTE, Role.ANALISTA):
        raise HTTPException(403, "No tienes permiso para generar estrategias.")
    return generate_strategy(db, id, user.id)


@router.post("/alertas/{id}/ejecutar", response_model=ExecutionResult)
def execute(id: UUID, db: DatabaseSession, user: CurrentUser):
    return execute_alert(db, id, user)


@router.get("/alertas/{id}/ejecuciones", response_model=list[ExecutionResponse])
def executions(id: UUID, db: DatabaseSession, user: CurrentUser):
    alert_by_id(id, db, user)
    return read_list(db, select(ExecutionAction).where(ExecutionAction.alert_id == id).order_by(ExecutionAction.created_at, ExecutionAction.id))
