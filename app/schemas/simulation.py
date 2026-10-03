from datetime import date

from pydantic import BaseModel


class SimulationResponse(BaseModel):
    fecha_actual: date
    fecha_maxima: date
