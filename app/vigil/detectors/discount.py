"""Detector determinístico de descuentos fuera de política.

Fuente funcional: resources/metricas.yaml → descuento_en_exceso. La unidad de
alerta es vendedor/semana y las cifras proceden exclusivamente de
centinela.v_descuentos_fuera_politica.
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, Numeric, bindparam, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.core import Alert, AlertStatus, Area, AuditLog


DISCOUNT_QUERY = text("""
    WITH violation_weeks AS (
        SELECT vendedor_id, date_trunc('week', fecha)::date AS semana
        FROM centinela.v_descuentos_fuera_politica
        WHERE fecha < :week + INTERVAL '7 days'
        GROUP BY vendedor_id, date_trunc('week', fecha)::date
    ), numbered AS (
        SELECT vendedor_id, semana,
               semana - (row_number() OVER (
                   PARTITION BY vendedor_id ORDER BY semana
               )::int * 7) AS island
        FROM violation_weeks
    ), streaks AS (
        SELECT vendedor_id, island, count(*) AS semanas_consecutivas
        FROM numbered GROUP BY vendedor_id, island
    ), current_streak AS (
        SELECT numbered.vendedor_id, streaks.semanas_consecutivas
        FROM numbered JOIN streaks USING (vendedor_id, island)
        WHERE numbered.semana = :week
    )
    SELECT violation.vendedor_id, :week AS semana,
           count(*) AS cantidad_lineas_fuera_politica,
           coalesce(sum(violation.descuento_en_exceso), 0) AS descuento_exceso_total,
           array_agg(DISTINCT violation.segmento ORDER BY violation.segmento) AS segmentos_afectados,
           count(DISTINCT violation.cliente_id) AS clientes_afectados,
           count(DISTINCT violation.pedido_id) AS pedidos_afectados,
           coalesce(current_streak.semanas_consecutivas, 1) AS semanas_consecutivas
    FROM centinela.v_descuentos_fuera_politica AS violation
    LEFT JOIN current_streak USING (vendedor_id)
    WHERE violation.fecha >= :week
      AND violation.fecha < :week + INTERVAL '7 days'
      AND violation.fecha <= :cutoff
    GROUP BY violation.vendedor_id, current_streak.semanas_consecutivas
    ORDER BY violation.vendedor_id
""").bindparams(
    bindparam("week", type_=Date()), bindparam("cutoff", type_=Date())
).columns(
    semana=Date(), descuento_exceso_total=Numeric(),
)


def discount_evidence(row) -> dict:
    streak = int(row["semanas_consecutivas"] or 1)
    amount = Decimal(row["descuento_exceso_total"] or 0)
    return {
        "vendedor_id": row["vendedor_id"],
        "semana": row["semana"].isoformat(),
        "cantidad_lineas_fuera_politica": int(row["cantidad_lineas_fuera_politica"]),
        "descuento_exceso_total": str(amount),
        "segmentos_afectados": list(row["segmentos_afectados"] or []),
        "clientes_afectados": int(row["clientes_afectados"]),
        "pedidos_afectados": int(row["pedidos_afectados"]),
        "reincidencia_semana_anterior": streak >= 2,
        "semanas_consecutivas": streak,
        "trigger_fuera_politica": True,
        "trigger_reincidencia": streak >= 2,
    }


class DiscountDetector:
    name = "discount_detector"

    def run(self, db: Session, user_id: UUID | None = None) -> int:
        db.execute(text("SELECT id FROM app.simulation_state WHERE id = 1 FOR SHARE"))
        cutoff = db.scalar(text("SELECT centinela.fecha_corte() AS cutoff").columns(cutoff=Date()))
        if cutoff is None:
            raise ValueError("Reloj simulado no inicializado")
        week = cutoff - timedelta(days=cutoff.weekday())
        rows = db.execute(DISCOUNT_QUERY, {"week": week, "cutoff": cutoff}).mappings().all()
        created = 0
        for row in rows:
            evidence = discount_evidence(row)
            key = json.dumps(
                ["DISCOUNT_POLICY_VIOLATION", row["vendedor_id"], week.isoformat()],
                ensure_ascii=False, separators=(",", ":"),
            )
            alert_id = db.scalar(insert(Alert).values(
                type="DISCOUNT_POLICY_VIOLATION", dedupe_key=key,
                area=Area.COMERCIAL,
                title=f"Descuentos fuera de política: {row['vendedor_id']}",
                summary="Se detectaron líneas con descuento superior al tope normal sin aprobación especial.",
                severity="CRITICAL" if evidence["trigger_reincidencia"] else "HIGH",
                status=AlertStatus.NEW, simulated_date=cutoff,
                confidence=None, amount_at_risk=None, root_cause=None,
                proposals=None, evidence=evidence,
            ).on_conflict_do_nothing(
                index_elements=[Alert.dedupe_key]
            ).returning(Alert.id))
            if alert_id is not None:
                db.add(AuditLog(
                    alert_id=alert_id, user_id=user_id,
                    event_type="ALERT_DETECTED",
                    payload={
                        "detector": self.name,
                        "type": "DISCOUNT_POLICY_VIOLATION",
                        "simulated_date": cutoff.isoformat(),
                        "evidence": evidence,
                    },
                ))
                created += 1
        return created
