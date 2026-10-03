from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MarginActionType(str, Enum):
    CREATE_PRICE_REVIEW_DRAFT = "CREATE_PRICE_REVIEW_DRAFT"
    CREATE_MARGIN_FOLLOWUP_TASK = "CREATE_MARGIN_FOLLOWUP_TASK"
    CREATE_SUPPLIER_REVIEW_TASK = "CREATE_SUPPLIER_REVIEW_TASK"


class InventoryActionType(str, Enum):
    CREATE_PURCHASE_ORDER_REVIEW_TASK = "CREATE_PURCHASE_ORDER_REVIEW_TASK"
    CREATE_SUPPLIER_FOLLOWUP_DRAFT = "CREATE_SUPPLIER_FOLLOWUP_DRAFT"
    CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK = "CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK"
    CREATE_URGENT_PARTIAL_DELIVERY_REVIEW = "CREATE_URGENT_PARTIAL_DELIVERY_REVIEW"


class ActionType(str, Enum):
    """Unión persistida en app.execution_actions (09_inventory_scenario.sql); S1 primero."""
    CREATE_PRICE_REVIEW_DRAFT = "CREATE_PRICE_REVIEW_DRAFT"
    CREATE_MARGIN_FOLLOWUP_TASK = "CREATE_MARGIN_FOLLOWUP_TASK"
    CREATE_SUPPLIER_REVIEW_TASK = "CREATE_SUPPLIER_REVIEW_TASK"
    CREATE_PURCHASE_ORDER_REVIEW_TASK = "CREATE_PURCHASE_ORDER_REVIEW_TASK"
    CREATE_SUPPLIER_FOLLOWUP_DRAFT = "CREATE_SUPPLIER_FOLLOWUP_DRAFT"
    CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK = "CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK"
    CREATE_URGENT_PARTIAL_DELIVERY_REVIEW = "CREATE_URGENT_PARTIAL_DELIVERY_REVIEW"

# Allowlist por escenario: el modelo solo ve y el Ejecutor solo acepta estas acciones.
ACTIONS_BY_ALERT_TYPE = {
    "MARGIN_ANOMALY": frozenset(m.value for m in MarginActionType),
    "INVENTORY_RISK": frozenset(m.value for m in InventoryActionType),
}


class ModelProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100)
    action_type: MarginActionType
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


class InventoryModelProposal(ModelProposal):
    action_type: InventoryActionType


class InventoryModelStrategy(ModelStrategy):
    proposals: list[InventoryModelProposal] = Field(min_length=1, max_length=3)


class Proposal(ModelProposal):
    action_type: ActionType
    financial_reference_cop: Decimal | None = Field(default=None, ge=0)


class Strategy(ModelStrategy):
    proposals: list[Proposal] = Field(min_length=1, max_length=3)
