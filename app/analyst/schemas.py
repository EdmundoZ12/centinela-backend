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


class InventorySnapshot(StrictModel):
    sku: str
    producto: str | None
    linea: str | None
    clase_abc: str | None
    bodega_id: str
    existencia: int | None
    demanda_prom_30d: Decimal | None
    cobertura_dias: Decimal | None
    cobertura_minima_dias: Decimal | None
    unidades_pendientes: Decimal | None
    origen_pendientes: str = "v_cobertura_inventario al corte de la alerta (evidencia del detector)"


class DemandAnalysis(StrictModel):
    ventana_actual_desde: date
    ventana_previa_desde: date
    dias_actuales: int
    dias_previos: int
    demanda_actual: Decimal | None
    demanda_historica: Decimal | None
    variacion_demanda_pct: Decimal | None
    entradas_ventana_actual: int
    salidas_ventana_actual: int


class DailyInventory(StrictModel):
    fecha: date
    entradas: int
    salidas: int
    existencia_final: int


class SupplierInfo(StrictModel):
    proveedor_id: str
    nombre: str | None
    lead_time_dias: int | None


class PurchaseOrderEvidence(StrictModel):
    orden_id: str
    proveedor_id: str
    proveedor: str | None
    cantidad: int
    fecha_orden: date
    fecha_esperada: date
    fecha_recibida: date | None
    estado_al_corte: Literal["RECIBIDA", "RETRASADA", "EN_TRANSITO"]
    dias_retraso: int | None
    origen: str = "ordenes_compra; estado derivado de fechas al corte, no del estado final del dataset"


class InventoryInvestigation(StrictModel):
    sku: str
    bodega_id: str
    fecha_corte: date
    inventory: InventorySnapshot
    demand_analysis: DemandAnalysis
    serie_reciente: list[DailyInventory]
    purchase_orders: list[PurchaseOrderEvidence]
    proveedor_catalogo: SupplierInfo | None
    aumento_demanda: bool | None
    ordenes_retrasadas: int
    unidades_retrasadas: int
    ordenes_en_transito: int
    unidades_en_transito: int
    cobertura_consistente_con_detector: bool
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
