from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from app.models.core import AlertStatus
from app.strategist.schemas import ActionType


class ExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    alert_id: UUID
    proposal_id: str
    action_type: ActionType
    payload: JsonValue
    result: JsonValue
    created_by: UUID
    created_at: datetime
    executed_at: datetime


class ExecutionResult(BaseModel):
    alert_status: AlertStatus
    actions: list[ExecutionResponse]
