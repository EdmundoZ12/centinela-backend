from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.simulation import router as simulation_router
from app.api.business import router as business_router
from app.schemas.health import HealthResponse

router = APIRouter()
router.include_router(simulation_router)
router.include_router(business_router)


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/database", tags=["health"])
def database_health(db: Annotated[Session, Depends(get_db)]) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection unavailable",
        ) from None
    return {"status": "ok", "database": "connected"}
