"""Investigación SQL fija y read-only para S4."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Date, Numeric, bindparam, text
from sqlalchemy.orm import Session

from app.analyst.schemas import (
    DiscountInvestigation, DiscountSeller, DiscountSummary, DiscountViolation,
)


VIOLATIONS_QUERY = text("""
    SELECT v.pedido_id, v.linea_n, v.cliente_id, v.segmento, v.sku,
           v.descuento_pct, v.tope_descuento_pct AS tope_normal_pct,
           limits.tope_especial_pct, v.aprobacion_especial,
           v.descuento_en_exceso, (v.valor_neto < v.costo_total) AS venta_debajo_costo
    FROM centinela.v_descuentos_fuera_politica v
    LEFT JOIN app.discount_policy_limits limits USING (segmento)
    WHERE v.vendedor_id=:seller AND v.fecha>=:week
      AND v.fecha<:week_end AND v.fecha<=:cutoff
    ORDER BY v.fecha, v.pedido_id, v.linea_n
""").bindparams(
    bindparam("week", type_=Date()), bindparam("week_end", type_=Date()),
    bindparam("cutoff", type_=Date()),
).columns(
    descuento_pct=Numeric(), tope_normal_pct=Numeric(),
    tope_especial_pct=Numeric(), descuento_en_exceso=Numeric(),
)

SPECIAL_EXCESS_QUERY = text("""
    SELECT v.pedido_id, v.linea_n, v.cliente_id, v.segmento, v.sku,
           v.descuento_pct, normal.tope_descuento_pct AS tope_normal_pct,
           limits.tope_especial_pct, v.aprobacion_especial,
           round(v.cantidad * v.precio_unitario *
                 (v.descuento_pct - limits.tope_especial_pct) / 100, 0) AS descuento_en_exceso,
           (v.valor_neto < v.costo_total) AS venta_debajo_costo
    FROM centinela.v_ventas v
    JOIN centinela.ref_topes_descuento normal USING (segmento)
    JOIN app.discount_policy_limits limits USING (segmento)
    WHERE v.vendedor_id=:seller AND v.fecha>=:week
      AND v.fecha<:week_end AND v.fecha<=:cutoff
      AND v.aprobacion_especial='S'
      AND v.descuento_pct>limits.tope_especial_pct
    ORDER BY v.fecha, v.pedido_id, v.linea_n
""").bindparams(
    bindparam("week", type_=Date()), bindparam("week_end", type_=Date()),
    bindparam("cutoff", type_=Date()),
).columns(
    descuento_pct=Numeric(), tope_normal_pct=Numeric(),
    tope_especial_pct=Numeric(), descuento_en_exceso=Numeric(),
)

SELLER_QUERY = text("""
    SELECT nombre FROM centinela.vendedores WHERE vendedor_id=:seller
""")


def _violation(row) -> DiscountViolation:
    return DiscountViolation(
        pedido_id=row["pedido_id"], linea_n=row["linea_n"],
        cliente_id=row["cliente_id"], segmento=row["segmento"], sku=row["sku"],
        descuento_pct=row["descuento_pct"],
        tope_normal_pct=row["tope_normal_pct"],
        tope_especial_pct=row["tope_especial_pct"],
        aprobacion_especial=row["aprobacion_especial"],
        descuento_en_exceso=row["descuento_en_exceso"],
        venta_debajo_costo=bool(row["venta_debajo_costo"]),
    )


def build_discount_investigation(
    seller_id: str, seller_name: str | None, week: date, cutoff: date,
    rows, special_rows, streak: int,
) -> DiscountInvestigation:
    violations = [_violation(row) for row in rows]
    special = [_violation(row) for row in special_rows]
    amount = sum((item.descuento_en_exceso for item in violations), Decimal("0"))
    limitations = [
        "Los pedidos cercanos no demuestran fraccionamiento deliberado ni intención de evadir la política.",
        "Una venta debajo del costo no demuestra ausencia de aprobación escrita de Gerencia General.",
        "El valor financiero es descuento observado por encima del tope aplicable, no pérdida garantizada.",
    ]
    return DiscountInvestigation(
        seller=DiscountSeller(vendedor_id=seller_id, nombre=seller_name, semana=week),
        fecha_corte=cutoff,
        summary=DiscountSummary(
            lineas_fuera_politica=len(violations),
            pedidos_afectados=len({v.pedido_id for v in violations}),
            clientes_afectados=len({v.cliente_id for v in violations}),
            descuento_exceso_total=amount,
            reincidencia=streak >= 2,
            semanas_consecutivas=streak,
        ),
        violations=violations,
        excesos_con_aprobacion_especial=special,
        limitaciones=limitations,
    )


def investigate_discount(
    db: Session, seller_id: str, week: date, cutoff: date, streak: int,
) -> DiscountInvestigation:
    if week > cutoff or cutoff >= week + timedelta(days=7):
        raise ValueError("Semana o corte inválidos")
    params = {
        "seller": seller_id, "week": week,
        "week_end": week + timedelta(days=7), "cutoff": cutoff,
    }
    rows = db.execute(VIOLATIONS_QUERY, params).mappings().all()
    special_rows = db.execute(SPECIAL_EXCESS_QUERY, params).mappings().all()
    seller_name = db.scalar(SELLER_QUERY, {"seller": seller_id})
    return build_discount_investigation(
        seller_id, seller_name, week, cutoff, rows, special_rows, streak,
    )
