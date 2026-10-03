"""Investigación determinística de inventario. Solo SELECT fijos y parametrizados.

El corte es el de la alerta, aunque el reloj ya haya avanzado. Ventanas:
actual = 30 días hasta el corte (misma definición que v_cobertura_inventario);
previa = los 60 días anteriores. El estado de cada orden de compra se deriva de
sus fechas al corte; la columna estado del dataset refleja el final y no se usa.
"""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Date, Numeric, bindparam, text
from sqlalchemy.orm import Session

from app.analyst.margin import percent_change
from app.analyst.schemas import (
    DailyInventory, DemandAnalysis, InventoryInvestigation, InventorySnapshot,
    PurchaseOrderEvidence, SupplierInfo,
)


CURRENT_DAYS = 30
PREVIOUS_DAYS = 60
RECENT_SERIES_DAYS = 14

PRODUCT_QUERY = text("""
    SELECT pr.sku, pr.nombre, pr.linea, pr.clase_abc, pr.proveedor_id,
           pv.nombre AS proveedor, pv.lead_time_dias
    FROM centinela.productos pr
    LEFT JOIN centinela.proveedores pv ON pv.proveedor_id = pr.proveedor_id
    WHERE pr.sku = :sku
""")

DAILY_QUERY = text("""
    SELECT fecha, entradas, salidas, existencia_final
    FROM centinela.inventario_diario
    WHERE sku = :sku AND bodega_id = :warehouse AND fecha >= :start AND fecha <= :cutoff
    ORDER BY fecha
""").bindparams(
    bindparam("start", type_=Date()), bindparam("cutoff", type_=Date())
).columns(fecha=Date())

PURCHASE_ORDERS_QUERY = text("""
    SELECT oc.oc_id, oc.proveedor_id, pv.nombre AS proveedor, oc.cantidad,
           oc.fecha_oc, oc.fecha_esperada, oc.fecha_recibida
    FROM centinela.ordenes_compra oc
    LEFT JOIN centinela.proveedores pv ON pv.proveedor_id = oc.proveedor_id
    WHERE oc.sku = :sku AND oc.bodega_id = :warehouse AND oc.fecha_oc <= :cutoff
      AND (oc.fecha_oc >= :start OR oc.fecha_recibida IS NULL OR oc.fecha_recibida > :cutoff)
    ORDER BY oc.fecha_oc, oc.oc_id
""").bindparams(
    bindparam("start", type_=Date()), bindparam("cutoff", type_=Date())
).columns(fecha_oc=Date(), fecha_esperada=Date(), fecha_recibida=Date())


def average(values: list[int]) -> Decimal | None:
    return Decimal(sum(values)) / Decimal(len(values)) if values else None


def order_evidence(row, cutoff: date) -> PurchaseOrderEvidence:
    received = row["fecha_recibida"] if row["fecha_recibida"] is not None and row["fecha_recibida"] <= cutoff else None
    if received is not None:
        status, delay = "RECIBIDA", max((received - row["fecha_esperada"]).days, 0)
    elif row["fecha_esperada"] < cutoff:
        # Política: una orden que no llega en la fecha esperada se marca como retrasada.
        status, delay = "RETRASADA", (cutoff - row["fecha_esperada"]).days
    else:
        status, delay = "EN_TRANSITO", None
    return PurchaseOrderEvidence(
        orden_id=row["oc_id"], proveedor_id=row["proveedor_id"], proveedor=row["proveedor"],
        cantidad=int(row["cantidad"]), fecha_orden=row["fecha_oc"], fecha_esperada=row["fecha_esperada"],
        fecha_recibida=received, estado_al_corte=status, dias_retraso=delay,
    )


def decimal_or_none(value) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def calculate_inventory_investigation(detection: dict, cutoff: date, product, daily, orders) -> InventoryInvestigation:
    current_start = cutoff - timedelta(days=CURRENT_DAYS - 1)
    previous_start = current_start - timedelta(days=PREVIOUS_DAYS)
    # Defensa adicional: descartar observaciones futuras aun si una fuente cambia.
    daily = [row for row in daily if row["fecha"] <= cutoff]
    orders = [row for row in orders if row["fecha_oc"] <= cutoff]
    current = [row for row in daily if row["fecha"] >= current_start]
    previous = [row for row in daily if previous_start <= row["fecha"] < current_start]
    current_demand = average([int(row["salidas"]) for row in current])
    previous_demand = average([int(row["salidas"]) for row in previous])
    variation = percent_change(previous_demand, current_demand)
    today = next((row for row in daily if row["fecha"] == cutoff), None)
    stock = int(today["existencia_final"]) if today is not None else None
    coverage = (Decimal(stock) / current_demand).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) \
        if stock is not None and current_demand else None
    order_items = [order_evidence(row, cutoff) for row in orders]
    late = [o for o in order_items if o.estado_al_corte == "RETRASADA"]
    transit = [o for o in order_items if o.estado_al_corte == "EN_TRANSITO"]
    limitations = [
        "El dataset no registra cantidades recibidas por orden; no se puede demostrar una entrega parcial.",
        "Los pedidos pendientes provienen de la vista oficial al corte de la detección.",
        "La comparación de demanda es descriptiva; no demuestra por sí sola causalidad.",
    ]
    if not previous:
        limitations.append("No hay demanda histórica comparable para el SKU y la bodega.")
    if today is None:
        limitations.append("No hay registro de inventario en la fecha de corte.")
    if not order_items:
        limitations.append("No hay órdenes de compra relacionadas en la ventana analizada.")
    detected_coverage = decimal_or_none(detection.get("cobertura_dias"))
    return InventoryInvestigation(
        sku=detection["sku"], bodega_id=detection["bodega_id"], fecha_corte=cutoff,
        inventory=InventorySnapshot(
            sku=detection["sku"], producto=product["nombre"] if product else detection.get("producto"),
            linea=product["linea"] if product else detection.get("linea"),
            clase_abc=(product["clase_abc"] or "").strip() if product else detection.get("clase_abc"),
            bodega_id=detection["bodega_id"], existencia=stock,
            demanda_prom_30d=current_demand.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if current_demand is not None else None,
            cobertura_dias=coverage, cobertura_minima_dias=decimal_or_none(detection.get("cobertura_minima_dias")),
            unidades_pendientes=decimal_or_none(detection.get("unidades_pendientes")),
        ),
        demand_analysis=DemandAnalysis(
            ventana_actual_desde=current_start, ventana_previa_desde=previous_start,
            dias_actuales=len(current), dias_previos=len(previous),
            demanda_actual=current_demand, demanda_historica=previous_demand, variacion_demanda_pct=variation,
            entradas_ventana_actual=sum(int(row["entradas"]) for row in current),
            salidas_ventana_actual=sum(int(row["salidas"]) for row in current),
        ),
        serie_reciente=[DailyInventory(fecha=row["fecha"], entradas=int(row["entradas"]), salidas=int(row["salidas"]),
                                       existencia_final=int(row["existencia_final"])) for row in daily[-RECENT_SERIES_DAYS:]],
        purchase_orders=order_items,
        proveedor_catalogo=SupplierInfo(proveedor_id=product["proveedor_id"], nombre=product["proveedor"],
                                        lead_time_dias=product["lead_time_dias"]) if product and product["proveedor_id"] else None,
        aumento_demanda=variation > 0 if variation is not None else None,
        ordenes_retrasadas=len(late), unidades_retrasadas=sum(o.cantidad for o in late),
        ordenes_en_transito=len(transit), unidades_en_transito=sum(o.cantidad for o in transit),
        cobertura_consistente_con_detector=coverage is not None and coverage == detected_coverage,
        limitaciones=limitations,
    )


def investigate_inventory(db: Session, detection: dict, cutoff: date) -> InventoryInvestigation:
    previous_start = cutoff - timedelta(days=CURRENT_DAYS + PREVIOUS_DAYS - 1)
    params = {"sku": detection["sku"], "warehouse": detection["bodega_id"], "start": previous_start, "cutoff": cutoff}
    product = db.execute(PRODUCT_QUERY, {"sku": detection["sku"]}).mappings().first()
    daily = db.execute(DAILY_QUERY, params).mappings().all()
    orders = db.execute(PURCHASE_ORDERS_QUERY, params).mappings().all()
    return calculate_inventory_investigation(detection, cutoff, product, daily, orders)
