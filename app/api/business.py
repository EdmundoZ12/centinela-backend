from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Select, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import CurrentUser
from app.core.permissions import require_alert_access, require_decision_role, visible_alerts
from app.db.session import get_db
from app.models.core import Alert, AlertStatus, AuditLog, DEMO_EMAILS, Decision, DecisionType, Role, User
from app.schemas.core import AlertResponse, AuditLogResponse, DecisionRequest, DecisionResponse, DecisionResult, UserResponse


router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_db)]


def read_list(db: Session, statement: Select) -> list:
    try:
        return list(db.scalars(statement).all())
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="No se pudo consultar la base de datos.") from None


@router.get("/users/demo", response_model=list[UserResponse], tags=["users"])
def demo_users(db: DatabaseSession):
    return read_list(db, select(User).where(User.email.in_(DEMO_EMAILS)).order_by(User.name, User.id))


@router.get("/alertas", response_model=list[AlertResponse], tags=["alertas"])
def alerts(db: DatabaseSession, user: CurrentUser):
    return read_list(db, select(Alert).where(visible_alerts(user)).order_by(Alert.detected_at.desc(), Alert.id))


@router.get("/alertas/{id}", response_model=AlertResponse, tags=["alertas"])
def alert_by_id(id: UUID, db: DatabaseSession, user: CurrentUser):
    try:
        alert = db.get(Alert, id)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="No se pudo consultar la base de datos.") from None
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada.")
    require_alert_access(user, alert)
    return alert


@router.get("/bitacora", response_model=list[AuditLogResponse], tags=["bitacora"])
def audit_log(
    db: DatabaseSession, user: CurrentUser,
    alert_id: UUID | None = None, event_type: str | None = None,
):
    statement = select(AuditLog)
    if user.role not in (Role.GERENTE, Role.AUDITOR):
        statement = statement.join(Alert, AuditLog.alert_id == Alert.id).where(visible_alerts(user))
    if alert_id is not None:
        statement = statement.where(AuditLog.alert_id == alert_id)
    if event_type is not None:
        statement = statement.where(AuditLog.event_type == event_type)
    return read_list(db, statement.order_by(AuditLog.created_at.desc(), AuditLog.id))


@router.post("/alertas/{id}/decision", response_model=DecisionResult, tags=["alertas"])
def decide_alert(id: UUID, body: DecisionRequest, db: DatabaseSession, user: CurrentUser):
    require_decision_role(user)
    try:
        # La consulta del usuario ya abrió la transacción de la sesión compartida.
        # Bloquear la alerta serializa decisiones concurrentes antes de validar su estado.
        alert = db.scalar(select(Alert).where(Alert.id == id).with_for_update())
        if alert is None:
            raise HTTPException(status_code=404, detail="Alerta no encontrada.")
        require_alert_access(user, alert)
        if alert.status != AlertStatus.PROPOSED:
            raise HTTPException(status_code=409, detail="Solo se puede decidir una alerta en estado PROPOSED.")
        previous_proposals = alert.proposals
        if body.decision == DecisionType.EDITED:
            if body.edited_proposal == previous_proposals:
                raise HTTPException(status_code=409, detail="La propuesta editada no contiene cambios.")
            alert.proposals = body.edited_proposal
        else:
            alert.status = AlertStatus(body.decision.value)

        decision = Decision(
            alert_id=alert.id, user_id=user.id, decision=body.decision,
            reason=body.reason, edited_proposal=body.edited_proposal,
        )
        db.add(decision)
        db.flush()  # Obtiene UUID y fecha de la decisión para enlazar la bitácora.
        payload = {
            "decision_id": str(decision.id), "decision": body.decision.value,
            "previous_status": AlertStatus.PROPOSED.value, "new_status": alert.status.value,
            "reason": body.reason, "simulated_date": alert.simulated_date.isoformat(),
        }
        if body.decision == DecisionType.EDITED:
            payload.update(previous_proposals=previous_proposals, edited_proposal=body.edited_proposal)
        db.add(AuditLog(
            alert_id=alert.id, user_id=user.id,
            event_type=f"ALERT_DECISION_{body.decision.value}", payload=payload,
        ))
        db.flush()
        result = DecisionResult(decision=DecisionResponse.model_validate(decision), alert_status=alert.status)
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="No se pudo registrar la decisión.") from None
