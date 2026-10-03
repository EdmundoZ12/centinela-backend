from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SupplierEvidence(StrictModel):
    proveedor_id: str
    nombre: str
    fecha_vigencia_actual: date
    fecha_vigencia_anterior: date | None
    costo_actual: Decimal
    costo_anterior: Decimal | None
    variacion_costo_pct: Decimal | None
    origen: str = "costos_proveedor; asociación de catálogo, no atribución de una compra concreta"


class SKUEvidence(StrictModel):
    sku: str
    producto: str | None = None
    costo_total_actual: Decimal | None = None
    margen_bruto_actual: Decimal | None = None
    margen_actual_pct: Decimal | None
    margen_historico_pct: Decimal | None
    caida_pp: Decimal | None
    ventas_actuales: Decimal
    unidades_actuales: Decimal
    costo_actual: Decimal | None
    costo_anterior: Decimal | None
    variacion_costo_pct: Decimal | None
    precio_actual: Decimal | None
    precio_anterior: Decimal | None
    variacion_precio_pct: Decimal | None
    aumento_costo: bool | None
    ajuste_precio: bool | None
    ajuste_precio_cubre_costo: bool | None
    contribucion_perdida_margen: Decimal | None
    proveedores: list[SupplierEvidence] = Field(default_factory=list)


class MarginInvestigation(StrictModel):
    linea: str
    semana: date
    fecha_corte: date
    semanas_historicas: list[date]
    margen_actual_pct: Decimal | None
    margen_historico_pct: Decimal | None
    caida_pp: Decimal | None
    margen_minimo_pct: Decimal | None = None
    sku_afectados: list[SKUEvidence]
    limitaciones: list[str]


class AnalysisFact(StrictModel):
    statement: str
    source: Literal["DATA", "POLICY"]


class PolicyFinding(StrictModel):
    statement: str
    document_name: str
    page_number: int | None
    chunk_index: int


class AnalysisExplanation(StrictModel):
    summary: str
    root_cause: str
    facts: list[AnalysisFact]
    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    interpretation: str
    confidence: float = Field(ge=0, le=1)
    insufficient_evidence: bool


class DiscountViolation(StrictModel):
    pedido_id: str
    linea_n: int
    cliente_id: str
    segmento: str
    sku: str
    descuento_pct: Decimal
    tope_normal_pct: Decimal
    tope_especial_pct: Decimal | None
    aprobacion_especial: str
    descuento_en_exceso: Decimal
    venta_debajo_costo: bool


class DiscountSeller(StrictModel):
    vendedor_id: str
    nombre: str | None
    semana: date


class DiscountSummary(StrictModel):
    lineas_fuera_politica: int
    pedidos_afectados: int
    clientes_afectados: int
    descuento_exceso_total: Decimal
    reincidencia: bool
    semanas_consecutivas: int


class DiscountInvestigation(StrictModel):
    seller: DiscountSeller
    fecha_corte: date
    summary: DiscountSummary
    violations: list[DiscountViolation]
    excesos_con_aprobacion_especial: list[DiscountViolation] = Field(default_factory=list)
    limitaciones: list[str] = Field(default_factory=list)
