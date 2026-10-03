from datetime import date

from pydantic import BaseModel

from app.schemas.vigil import VigilSummary


class SimulationResponse(BaseModel):
    fecha_actual: date
    fecha_maxima: date


class SimulationAdvanceResponse(SimulationResponse):
    vigia: VigilSummary
