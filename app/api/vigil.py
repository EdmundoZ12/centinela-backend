from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dependencies import CurrentUser
from app.db.session import get_db
from app.models.core import Role
from app.schemas.vigil import VigilSummary
from app.vigil.service import run_vigil


router = APIRouter(prefix="/vigia", tags=["vigia"])


@router.post("/ejecutar", response_model=VigilSummary)
def execute_vigil(db: Annotated[Session, Depends(get_db)], user: CurrentUser):
    if user.role not in (Role.GERENTE, Role.ANALISTA):
        raise HTTPException(status_code=403, detail="No tienes permiso para ejecutar el Vigía.")
    return run_vigil(db, user_id=user.id)
