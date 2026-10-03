"""Investigación determinística. Todas las consultas son SELECT fijos y parametrizados.

El corte es el de la alerta, incluso si el reloj ya avanzó. La ventana histórica
son las ocho semanas anteriores disponibles de la línea (regla del detector).
"""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Date, Numeric, bindparam, text
from sqlalchemy.orm import Session

from app.analyst.schemas import MarginInvestigation, SKUEvidence, SupplierEvidence


SALES_QUERY = text("""
    SELECT sku, producto, fecha, cantidad, valor_neto, costo_total
    FROM centinela.v_ventas
    WHERE linea=:line AND fecha>=:start AND fecha<=:cutoff
    ORDER BY fecha, sku
""").bindparams(
    bindparam("start", type_=Date()), bindparam("cutoff", type_=Date())
).columns(fecha=Date(), cantidad=Numeric(), valor_neto=Numeric(), costo_total=Numeric())

HISTORY_QUERY = text("""
    SELECT DISTINCT semana FROM centinela.v_margen_semanal_linea
    WHERE linea=:line AND semana<:week AND margen_pct IS NOT NULL
    ORDER BY semana DESC LIMIT 8
""").bindparams(bindparam("week", type_=Date())).columns(semana=Date())

SUPPLIERS_QUERY = text("""
    WITH ranked AS (
        SELECT c.sku, c.proveedor_id, p.nombre, c.fecha_vigencia, c.costo_unitario,
               row_number() OVER (PARTITION BY c.sku, c.proveedor_id ORDER BY c.fecha_vigencia DESC) AS position
        FROM centinela.costos_proveedor c
        JOIN centinela.proveedores p USING (proveedor_id)
        JOIN centinela.productos pr USING (sku)
        WHERE pr.linea=:line AND c.fecha_vigencia<=:cutoff
    )
    SELECT * FROM ranked WHERE position<=2 ORDER BY sku, proveedor_id, position
""").bindparams(bindparam("cutoff", type_=Date())).columns(fecha_vigencia=Date(), costo_unitario=Numeric())


def percent_change(previous: Decimal | None, current: Decimal | None) -> Decimal | None:
    if previous is None or previous == 0 or current is None:
        return None
    return (current - previous) * Decimal("100") / previous


def margin(values) -> Decimal | None:
    sales = sum((row["valor_neto"] for row in values), Decimal("0"))
    costs = sum((row["costo_total"] for row in values), Decimal("0"))
    return ((sales - costs) * Decimal("100") / sales).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if sales else None


def unit_values(rows):
    units = sum((row["cantidad"] for row in rows), Decimal("0"))
    sales = sum((row["valor_neto"] for row in rows), Decimal("0"))
    costs = sum((row["costo_total"] for row in rows), Decimal("0"))
    return units, sales, costs / units if units else None, sales / units if units else None


def calculate_investigation(line: str, week: date, cutoff: date, weeks: list[date], rows, suppliers) -> MarginInvestigation:
    # Defensa adicional: descartar observaciones futuras aun si una fuente cambia.
    rows = [row for row in rows if row["fecha"] <= cutoff]
    current_rows = [row for row in rows if week <= row["fecha"] < week + timedelta(days=7)]
    history_rows = [row for row in rows if row["fecha"] - timedelta(days=row["fecha"].weekday()) in weeks]
    weekly_margins = [margin([row for row in history_rows if start <= row["fecha"] < start + timedelta(days=7)]) for start in weeks]
    valid = [value for value in weekly_margins if value is not None]
    average = sum(valid) / Decimal(len(valid)) if valid else None
    current_margin = margin(current_rows)
    sku_evidence = []
    for sku in sorted({row["sku"] for row in current_rows}):
        current = [row for row in current_rows if row["sku"] == sku]
        prior = [row for row in history_rows if row["sku"] == sku]
        units, sales, cost, price = unit_values(current)
        _, _, old_cost, old_price = unit_values(prior)
        sku_margins = [margin([row for row in prior if start <= row["fecha"] < start + timedelta(days=7)]) for start in weeks]
        available = [value for value in sku_margins if value is not None]
        historic_margin = sum(available) / Decimal(len(available)) if available else None
        actual_margin = margin(current)
        cost_change = percent_change(old_cost, cost)
        price_change = percent_change(old_price, price)
        supplier_evidence = []
        for provider in sorted({row["proveedor_id"] for row in suppliers if row["sku"] == sku and row["fecha_vigencia"] <= cutoff}):
            records = sorted([row for row in suppliers if row["sku"] == sku and row["proveedor_id"] == provider and row["fecha_vigencia"] <= cutoff], key=lambda row: row["fecha_vigencia"], reverse=True)
            latest = records[0]
            previous = records[1] if len(records) > 1 else None
            supplier_evidence.append(SupplierEvidence(
                proveedor_id=provider, nombre=latest["nombre"], fecha_vigencia_actual=latest["fecha_vigencia"],
                fecha_vigencia_anterior=previous["fecha_vigencia"] if previous else None,
                costo_actual=latest["costo_unitario"], costo_anterior=previous["costo_unitario"] if previous else None,
                variacion_costo_pct=percent_change(previous["costo_unitario"] if previous else None, latest["costo_unitario"]),
            ))
        sku_evidence.append(SKUEvidence(
            producto=current[0].get("producto"),
            costo_total_actual=sum((r["costo_total"] for r in current), Decimal("0")),
            margen_bruto_actual=sales-sum((r["costo_total"] for r in current), Decimal("0")),
            sku=sku, margen_actual_pct=actual_margin, margen_historico_pct=historic_margin,
            caida_pp=historic_margin - actual_margin if historic_margin is not None and actual_margin is not None else None,
            ventas_actuales=sales, unidades_actuales=units, costo_actual=cost, costo_anterior=old_cost,
            variacion_costo_pct=cost_change, precio_actual=price, precio_anterior=old_price,
            variacion_precio_pct=price_change, aumento_costo=cost_change > 0 if cost_change is not None else None,
            ajuste_precio=price_change > 0 if price_change is not None else None,
            ajuste_precio_cubre_costo=price_change >= cost_change if price_change is not None and cost_change is not None and cost_change > 0 else None,
            contribucion_perdida_margen=sales * (historic_margin - actual_margin) / Decimal("100") if historic_margin is not None and actual_margin is not None else None,
            proveedores=supplier_evidence,
        ))
    sku_evidence.sort(key=lambda item: (item.contribucion_perdida_margen is None, -(item.contribucion_perdida_margen or Decimal("0")), item.sku))
    limitations = [
        "Comparación descriptiva; no demuestra por sí sola causalidad.",
        "Costos y precios de ventas son promedios ponderados por unidades; pueden variar por mezcla y descuentos.",
        "La contribución es ventas actuales por la caída de margen del SKU; no incluye cambios en la mezcla entre SKU.",
        "El proveedor del catálogo no demuestra quién suministró una venta concreta.",
    ]
    if not valid:
        limitations.append("No hay margen histórico comparable de la línea.")
    if not current_rows:
        limitations.append("No hay ventas actuales hasta el corte de la alerta.")
    if any(item.margen_historico_pct is None for item in sku_evidence):
        limitations.append("Hay SKU sin historia comparable.")
    return MarginInvestigation(
        linea=line, semana=week, fecha_corte=cutoff, semanas_historicas=weeks,
        margen_actual_pct=current_margin, margen_historico_pct=average,
        caida_pp=average-current_margin if average is not None and current_margin is not None else None,
        sku_afectados=sku_evidence, limitaciones=limitations,
    )


def investigate_margin(db: Session, line: str, week: date, cutoff: date) -> MarginInvestigation:
    if week > cutoff:
        raise ValueError("Semana posterior al corte")
    weeks = db.execute(HISTORY_QUERY, {"line": line, "week": week}).scalars().all()
    start = min(weeks) if weeks else week
    rows = db.execute(SALES_QUERY, {"line": line, "start": start, "cutoff": cutoff}).mappings().all()
    suppliers = db.execute(SUPPLIERS_QUERY, {"line": line, "cutoff": cutoff}).mappings().all()
    evidence = calculate_investigation(line, week, cutoff, weeks, rows, suppliers)
    evidence.margen_minimo_pct = db.scalar(text(
        "SELECT margen_minimo_pct FROM centinela.ref_margen_minimo_linea WHERE linea=:line"
    ).columns(margen_minimo_pct=Numeric()), {"line": line})
    return evidence
