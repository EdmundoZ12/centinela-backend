from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.core import User


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> User:
    try:
        user_id = UUID(x_user_id) if x_user_id else None
    except ValueError:
        user_id = None
    if user_id is None:
        raise HTTPException(status_code=401, detail="Usuario actual no válido.")
    try:
        user = db.get(User, user_id)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="No se pudo verificar el usuario actual.") from None
    if user is None:
        raise HTTPException(status_code=401, detail="Usuario actual no válido.")
    if not user.active:
        raise HTTPException(status_code=403, detail="Acceso no permitido.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
