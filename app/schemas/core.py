from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from app.models.core import AlertStatus, Area, DecisionType, Role


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserResponse(ORMResponse):
    id: UUID
    name: str
    email: str
    role: Role
    area: Area | None
    active: bool
    created_at: datetime


class AlertResponse(ORMResponse):
    id: UUID
    type: str
    area: Area
    title: str
    summary: str
    severity: str
    confidence: Decimal | None
    amount_at_risk: Decimal | None
    status: AlertStatus
    simulated_date: date
    detected_at: datetime
    evidence: JsonValue
    root_cause: JsonValue | None
    proposals: JsonValue | None
    created_at: datetime
    updated_at: datetime


class AuditLogResponse(ORMResponse):
    id: UUID
    alert_id: UUID | None
    user_id: UUID | None
    event_type: str
    payload: JsonValue
    created_at: datetime


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionType
    reason: str | None = None
    edited_proposal: JsonValue | None = None

    @model_validator(mode="after")
    def validate_proposal(self):
        if self.decision == DecisionType.EDITED:
            if not isinstance(self.edited_proposal, (dict, list)) or not self.edited_proposal:
                raise ValueError("EDITED requiere edited_proposal como objeto o lista JSON no vacía.")
        elif self.edited_proposal is not None:
            raise ValueError("edited_proposal solo se admite para EDITED.")
        return self


class DecisionResponse(ORMResponse):
    id: UUID
    alert_id: UUID
    user_id: UUID
    decision: DecisionType
    reason: str | None
    edited_proposal: JsonValue | None
    created_at: datetime


class DecisionResult(BaseModel):
    decision: DecisionResponse
    alert_status: AlertStatus
