from fastapi import HTTPException
from sqlalchemy import false, true

from app.models.core import Alert, Role, User


def visible_alerts(user: User):
    if user.role == Role.LIDER_PROCESO:
        return Alert.area == user.area if user.area is not None else false()
    if user.role in (Role.GERENTE, Role.ANALISTA, Role.AUDITOR):
        return true()
    raise HTTPException(status_code=403, detail="Acceso no permitido.")


def require_alert_access(user: User, alert: Alert) -> None:
    if user.role == Role.LIDER_PROCESO:
        if user.area is None or user.area != alert.area:
            raise HTTPException(status_code=403, detail="Acceso no permitido.")
    elif user.role not in (Role.GERENTE, Role.ANALISTA, Role.AUDITOR):
        raise HTTPException(status_code=403, detail="Acceso no permitido.")


def require_decision_role(user: User) -> None:
    if user.role not in (Role.GERENTE, Role.LIDER_PROCESO):
        raise HTTPException(status_code=403, detail="No tienes permiso para decidir alertas.")
