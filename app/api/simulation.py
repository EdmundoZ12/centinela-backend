from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.simulation import SimulationResponse


router = APIRouter(prefix="/simulacion", tags=["simulacion"])
MAX_DATE = date(2026, 9, 30)
MAX_ADVANCE_DAYS = (MAX_DATE - date(2026, 6, 30)).days
DatabaseSession = Annotated[Session, Depends(get_db)]


def response(current: date | None) -> SimulationResponse:
    if current is None:
        raise HTTPException(status_code=503, detail="El reloj simulado no está inicializado.")
    return SimulationResponse(fecha_actual=current, fecha_maxima=MAX_DATE)


@router.get("", response_model=SimulationResponse)
def get_simulation(db: DatabaseSession) -> SimulationResponse:
    try:
        current = db.execute(text(
            'SELECT "current_date" FROM app.simulation_state WHERE id = 1'
        )).scalar_one_or_none()
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="No se pudo consultar el reloj simulado.") from None
    return response(current)


@router.post("/avanzar", response_model=SimulationResponse)
def advance_simulation(
    db: DatabaseSession, dias: Annotated[int, Query(gt=0)] = 1,
) -> SimulationResponse:
    try:
        # UPDATE toma el bloqueo de fila: avances concurrentes no pierden incrementos.
        current = db.execute(text(
            'UPDATE app.simulation_state '
            'SET "current_date" = LEAST("current_date" + :days, :maximum), '
            'updated_at = clock_timestamp() WHERE id = 1 RETURNING "current_date"'
        ), {"days": min(dias, MAX_ADVANCE_DAYS), "maximum": MAX_DATE}).scalar_one_or_none()
        result = response(current)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="No se pudo avanzar el reloj simulado.") from None
    return result
