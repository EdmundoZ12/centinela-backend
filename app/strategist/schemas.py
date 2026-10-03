from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionType(str, Enum):
    CREATE_PRICE_REVIEW_DRAFT = "CREATE_PRICE_REVIEW_DRAFT"
    CREATE_MARGIN_FOLLOWUP_TASK = "CREATE_MARGIN_FOLLOWUP_TASK"
    CREATE_SUPPLIER_REVIEW_TASK = "CREATE_SUPPLIER_REVIEW_TASK"


class ModelProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100)
    action_type: ActionType
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    priority: Literal["HIGH", "MEDIUM", "LOW"]


class ModelStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)
    proposals: list[ModelProposal] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({p.id for p in self.proposals}) != len(self.proposals):
            raise ValueError("IDs de propuesta duplicados")
        return self


class Proposal(ModelProposal):
    financial_reference_cop: Decimal | None = Field(default=None, ge=0)


class Strategy(ModelStrategy):
    proposals: list[Proposal] = Field(min_length=1, max_length=3)
