from pydantic import BaseModel, Field, model_serializer


class VigilSummary(BaseModel):
    detectores_ejecutados: int = 0
    alertas_nuevas: int = 0
    errores: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        if not self.errores:
            result.pop("errores", None)
        return result
