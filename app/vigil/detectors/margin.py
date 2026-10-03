"""Regla oficial: resources/metricas.yaml → metricas.margen_pct.

Vista v_margen_semanal_linea; dimensiones semana/linea. Caída > 3 puntos
frente al promedio de 8 semanas previas o margen bajo ref_margen_minimo_linea.
La vista oficial entrega porcentajes (100 × la fórmula del YAML).
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, Numeric, bindparam, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.core import Alert, AlertStatus, Area, AuditLog


MARGIN_QUERY = text("""
    WITH history AS (
        SELECT linea, margen_pct,
               row_number() OVER (PARTITION BY linea ORDER BY semana DESC) AS position
        FROM centinela.v_margen_semanal_linea
        WHERE semana < :week AND margen_pct IS NOT NULL
    ), averages AS (
        SELECT linea, avg(margen_pct) AS margen_promedio_8_semanas_pct
        FROM history WHERE position <= 8 GROUP BY linea
    )
    SELECT current.linea, current.semana,
           current.margen_pct AS margen_actual_pct,
           averages.margen_promedio_8_semanas_pct, minimum.margen_minimo_pct
    FROM centinela.v_margen_semanal_linea AS current
    LEFT JOIN averages ON averages.linea = current.linea
    LEFT JOIN centinela.ref_margen_minimo_linea AS minimum ON minimum.linea = current.linea
    WHERE current.semana = :week AND current.margen_pct IS NOT NULL
    ORDER BY current.linea
""").bindparams(bindparam("week", type_=Date())).columns(
    semana=Date(), margen_actual_pct=Numeric(),
    margen_promedio_8_semanas_pct=Numeric(), margen_minimo_pct=Numeric(),
)


def margin_evidence(row) -> dict | None:
    current = row["margen_actual_pct"]
    average = row["margen_promedio_8_semanas_pct"]
    minimum = row["margen_minimo_pct"]
    drop = average - current if average is not None else None
    trigger_drop = drop is not None and drop > Decimal("3")
    trigger_minimum = minimum is not None and current < minimum
    if not (trigger_drop or trigger_minimum):
        return None
    return {
        "linea": row["linea"], "semana": row["semana"].isoformat(),
        "margen_actual_pct": float(current),
        "margen_promedio_8_semanas_pct": float(average) if average is not None else None,
        "caida_pp": float(drop) if drop is not None else None,
        "margen_minimo_pct": float(minimum) if minimum is not None else None,
        "trigger_caida": trigger_drop, "trigger_margen_minimo": trigger_minimum,
    }


class MarginDetector:
    name = "margin_detector"

    def run(self, db: Session, user_id: UUID | None = None) -> int:
        # Mantener el corte estable mientras se lee la vista y se persisten alertas.
        db.execute(text("SELECT id FROM app.simulation_state WHERE id = 1 FOR SHARE"))
        cutoff = db.scalar(text("SELECT centinela.fecha_corte() AS cutoff").columns(cutoff=Date()))
        if cutoff is None:
            raise ValueError("Reloj simulado no inicializado")
        week = cutoff - timedelta(days=cutoff.weekday())
        rows = db.execute(MARGIN_QUERY, {"week": week}).mappings().all()
        created = 0
        for row in rows:
            evidence = margin_evidence(row)
            if evidence is None:
                continue
            # JSON representa la tupla sin colisiones por separadores en nombres de línea.
            key = json.dumps(["MARGIN_ANOMALY", row["linea"], week.isoformat()], ensure_ascii=False, separators=(",", ":"))
            alert_id = db.scalar(insert(Alert).values(
                type="MARGIN_ANOMALY", dedupe_key=key, area=Area.COMERCIAL,
                title=f"Anomalía de margen: {row['linea']}",
                summary=f"Margen semanal {evidence['margen_actual_pct']}%; supera el umbral de caída o está bajo el mínimo de la línea.",
                severity="HIGH", status=AlertStatus.NEW, simulated_date=cutoff,
                confidence=None, amount_at_risk=None, root_cause=None, proposals=None,
                evidence=evidence,
            ).on_conflict_do_nothing(index_elements=[Alert.dedupe_key]).returning(Alert.id))
            if alert_id is not None:
                db.add(AuditLog(
                    alert_id=alert_id, user_id=user_id, event_type="ALERT_DETECTED",
                    payload={"detector": self.name, "type": "MARGIN_ANOMALY", "simulated_date": cutoff.isoformat(), "evidence": evidence},
                ))
                created += 1
        return created
