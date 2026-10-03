"""Regla oficial: resources/metricas.yaml → metricas.cobertura_dias.

Vista v_cobertura_inventario (ya acotada por fecha_corte()); dimensiones SKU/bodega.
Umbral del YAML: < 10 días en clase A; crítico < 5 días con pedidos pendientes.
Mínimos por clase de la política de inventario (OPE-POL-007): A 10, B 7, C 5 días.
La severidad es determinística; la vista entrega cobertura redondeada a un decimal.
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, Numeric, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.core import Alert, AlertStatus, Area, AuditLog


MINIMUM_COVERAGE_DAYS = {"A": Decimal("10"), "B": Decimal("7"), "C": Decimal("5")}
CRITICAL_COVERAGE_DAYS = Decimal("5")

INVENTORY_QUERY = text("""
    SELECT sku, nombre, linea, clase_abc, bodega_id, existencia,
           demanda_prom_30d, cobertura_dias, unidades_pendientes
    FROM centinela.v_cobertura_inventario
    WHERE cobertura_dias IS NOT NULL
    ORDER BY sku, bodega_id
""").columns(demanda_prom_30d=Numeric(), cobertura_dias=Numeric(), unidades_pendientes=Numeric())


def inventory_evidence(row, cutoff) -> dict | None:
    coverage = row["cobertura_dias"]
    minimum = MINIMUM_COVERAGE_DAYS.get((row["clase_abc"] or "").strip())
    pending = row["unidades_pendientes"] or Decimal("0")
    trigger_coverage = minimum is not None and coverage < minimum
    trigger_critical = coverage < CRITICAL_COVERAGE_DAYS and pending > 0
    if not (trigger_coverage or trigger_critical):
        return None
    return {
        "sku": row["sku"], "producto": row["nombre"], "linea": row["linea"],
        "clase_abc": (row["clase_abc"] or "").strip(), "bodega_id": row["bodega_id"],
        "fecha_corte": cutoff.isoformat(),
        "existencia": int(row["existencia"]),
        "demanda_prom_30d": float(row["demanda_prom_30d"]),
        "cobertura_dias": float(coverage),
        "cobertura_minima_dias": float(minimum) if minimum is not None else None,
        "unidades_pendientes": float(pending),
        "trigger_cobertura": trigger_coverage, "trigger_critico": trigger_critical,
    }


def severity(evidence: dict) -> str:
    # El contrato de severity es texto libre; CRITICAL solo existe para el caso crítico oficial.
    return "CRITICAL" if evidence["trigger_critico"] else "HIGH"


class InventoryDetector:
    name = "inventory_detector"

    def run(self, db: Session, user_id: UUID | None = None) -> int:
        # Mantener el corte estable mientras se lee la vista y se persisten alertas.
        db.execute(text("SELECT id FROM app.simulation_state WHERE id = 1 FOR SHARE"))
        cutoff = db.scalar(text("SELECT centinela.fecha_corte() AS cutoff").columns(cutoff=Date()))
        if cutoff is None:
            raise ValueError("Reloj simulado no inicializado")
        week = cutoff - timedelta(days=cutoff.weekday())
        created = 0
        for row in db.execute(INVENTORY_QUERY).mappings().all():
            evidence = inventory_evidence(row, cutoff)
            if evidence is None:
                continue
            level = severity(evidence)
            # Una semana por nivel: re-ejecutar no duplica; escalar a CRITICAL o una semana nueva sí alerta.
            key = json.dumps(["INVENTORY_RISK", row["sku"], row["bodega_id"], level, week.isoformat()],
                             ensure_ascii=False, separators=(",", ":"))
            summary = (f"Cobertura de {evidence['cobertura_dias']} días; mínimo de clase "
                       f"{evidence['clase_abc']}: {evidence['cobertura_minima_dias']} días.")
            if evidence["trigger_critico"]:
                summary += f" Crítico: {evidence['unidades_pendientes']:g} unidades pendientes de despacho."
            alert_id = db.scalar(insert(Alert).values(
                type="INVENTORY_RISK", dedupe_key=key, area=Area.INVENTARIO,
                title=f"Riesgo de inventario: {row['nombre']} en {row['bodega_id']}",
                summary=summary, severity=level, status=AlertStatus.NEW, simulated_date=cutoff,
                confidence=None, amount_at_risk=None, root_cause=None, proposals=None,
                evidence=evidence,
            ).on_conflict_do_nothing(index_elements=[Alert.dedupe_key]).returning(Alert.id))
            if alert_id is not None:
                db.add(AuditLog(
                    alert_id=alert_id, user_id=user_id, event_type="ALERT_DETECTED",
                    payload={"detector": self.name, "type": "INVENTORY_RISK", "simulated_date": cutoff.isoformat(), "evidence": evidence},
                ))
                created += 1
        return created
