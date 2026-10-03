"""S3 sobre PostgreSQL real y dataset oficial; OpenAI mockeado.

1. Reloj → Vigía → InventoryDetector sobre todo el rango simulado, en una transacción
   que siempre termina en rollback. Descubre el escenario sin conocer SKU, bodega,
   proveedor ni fecha.
2. Flujo end-to-end por la API con limpieza por UUID propio (requiere 09_inventory_scenario.sql).
"""
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.analyst.inventory import investigate_inventory
from app.analyst.schemas import AnalysisExplanation
from app.api.simulation import MAX_ADVANCE_DAYS, MAX_DATE, advance_simulation
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AlertStatus, Area, AuditLog, Decision, ExecutionAction, Role, User
from app.rag.service import PolicyReference
from app.vigil.detectors.inventory import INVENTORY_QUERY, MINIMUM_COVERAGE_DAYS, InventoryDetector, inventory_evidence
from app.vigil.service import run_vigil
from test_inventory import inventory_strategy

ROOT = Path(__file__).resolve().parents[1]


def engine():
    url = make_url(dotenv_values(ROOT / ".env")["DATABASE_URL_UNPOOLED"]).set(drivername="postgresql+psycopg")
    return create_engine(url, connect_args={"connect_timeout": 15})


@unittest.skipUnless(os.environ.get("CENTINELA_TEST_POSTGRES") == "1", "Requiere CENTINELA_TEST_POSTGRES=1")
class InventoryPostgresTests(unittest.TestCase):
    @patch("app.vigil.service.DETECTORS", (InventoryDetector(),))
    def test_clock_detects_official_scenario_without_duplicates_or_future_data(self):
        db_engine = engine()
        try:
            with db_engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
                        before = set(db.scalars(select(Alert.id)).all())
                        start = MAX_DATE - timedelta(days=MAX_ADVANCE_DAYS)
                        db.execute(text('UPDATE app.simulation_state SET "current_date"=:d WHERE id=1'), {"d": start})
                        db.commit()
                        self.assertEqual(run_vigil(db).errores, [])
                        while (advanced := advance_simulation(db=db, dias=1)).fecha_actual < MAX_DATE:
                            self.assertEqual(advanced.vigia.errores, [])
                        self.assertEqual(run_vigil(db).alertas_nuevas, 0, "Re-ejecutar el Vigía no debe duplicar alertas.")

                        # La vista oficial no expone inventario posterior al corte.
                        self.assertEqual(db.scalar(text(
                            "SELECT count(*) FROM centinela.v_cobertura_inventario v WHERE NOT EXISTS ("
                            "SELECT 1 FROM centinela.inventario_diario i WHERE i.sku=v.sku AND i.bodega_id=v.bodega_id "
                            "AND i.fecha=centinela.fecha_corte())")), 0)

                        alerts = db.scalars(select(Alert).where(Alert.id.not_in(before), Alert.type == "INVENTORY_RISK")).all()
                        self.assertTrue(alerts, "El rango simulado debe producir alertas de inventario.")
                        self.assertEqual(len({a.dedupe_key for a in alerts}), len(alerts))
                        for alert in alerts:
                            evidence = alert.evidence
                            self.assertEqual((alert.area, alert.status), (Area.INVENTARIO, AlertStatus.NEW))
                            self.assertEqual(evidence["fecha_corte"], alert.simulated_date.isoformat())
                            minimum = MINIMUM_COVERAGE_DAYS.get(evidence["clase_abc"])
                            self.assertEqual(evidence["trigger_cobertura"], minimum is not None and evidence["cobertura_dias"] < minimum)
                            self.assertEqual(evidence["trigger_critico"], evidence["cobertura_dias"] < 5 and evidence["unidades_pendientes"] > 0)
                            self.assertEqual(alert.severity, "CRITICAL" if evidence["trigger_critico"] else "HIGH")
                            logs = db.scalars(select(AuditLog).where(AuditLog.alert_id == alert.id)).all()
                            self.assertEqual([log.event_type for log in logs], ["ALERT_DETECTED"])

                        critical = [a for a in alerts if a.severity == "CRITICAL"]
                        self.assertTrue(critical, "El dataset oficial contiene un caso crítico: cobertura < 5 con pedidos pendientes.")
                        explained = []
                        for alert in critical:
                            investigation = investigate_inventory(db, alert.evidence, alert.simulated_date)
                            # Reproducción independiente: cobertura recalculada desde inventario_diario.
                            self.assertTrue(investigation.cobertura_consistente_con_detector)
                            self.assertTrue(all(day.fecha <= alert.simulated_date for day in investigation.serie_reciente))
                            for order in investigation.purchase_orders:
                                self.assertLessEqual(order.fecha_orden, alert.simulated_date)
                                self.assertTrue(order.fecha_recibida is None or order.fecha_recibida <= alert.simulated_date)
                            if investigation.ordenes_retrasadas and investigation.aumento_demanda:
                                explained.append(investigation)
                        # Patrón del generador: demanda creciente + reposición retrasada → cobertura crítica.
                        self.assertTrue(explained, "Debe demostrarse con SQL aumento de demanda y orden retrasada.")
                        self.assertTrue(all(i.proveedor_catalogo or i.purchase_orders for i in explained))
                        self.assertTrue(db.scalar(text(
                            "SELECT count(*) > 0 FROM app.policy_chunks WHERE document_name ILIKE '%inventario%' "
                            "AND content ILIKE '%cobertura%'")), "La política de inventario debe estar indexada.")
                finally:
                    transaction.rollback()
        finally:
            db_engine.dispose()

    def test_api_flow_new_to_executed_and_cleanup(self):
        db_engine = engine()
        alert_id, user_id = uuid4(), uuid4()

        def session():
            with Session(db_engine) as db:
                yield db

        client = MagicMock()
        client.responses.parse.side_effect = [
            type("Response", (), {"status": "completed", "output_parsed": AnalysisExplanation(
                summary="Análisis descriptivo", root_cause="La causa no está demostrada.", facts=[],
                interpretation="Requiere revisión humana de la evidencia.", confidence=.6, insufficient_evidence=True)})(),
            type("Response", (), {"status": "completed", "output_parsed": inventory_strategy()})(),
        ]
        try:
            with Session(db_engine) as db:
                definition = db.scalar(text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname='execution_actions_action_type_check'"))
                self.assertIn("CREATE_SUPPLIER_FOLLOWUP_DRAFT", definition or "",
                              "Aplicar database/scripts/apply_inventory_scenario.py antes de esta prueba.")
                # Buscar hacia atrás, en una transacción revertida, el último corte con riesgo real.
                current = db.scalar(text("SELECT centinela.fecha_corte()"))
                candidate, cutoff = None, current
                while candidate is None and cutoff >= MAX_DATE - timedelta(days=MAX_ADVANCE_DAYS):
                    db.execute(text('UPDATE app.simulation_state SET "current_date"=:d WHERE id=1'), {"d": cutoff})
                    candidate = next((e for e in (inventory_evidence(r, cutoff) for r in db.execute(INVENTORY_QUERY).mappings()) if e), None)
                    cutoff = cutoff if candidate else cutoff - timedelta(days=1)
                db.rollback()
                self.assertEqual(db.scalar(text("SELECT centinela.fecha_corte()")), current)
                self.assertIsNotNone(candidate, "Debe existir un riesgo real de inventario hasta el corte actual.")
                db.add(User(id=user_id, name="Fixture S3", email=f"{user_id}@test.invalid", role=Role.GERENTE, active=True))
                db.add(Alert(id=alert_id, type="INVENTORY_RISK", area=Area.INVENTARIO, title="Fixture S3",
                             summary="Fixture basada en métrica oficial", severity="HIGH", status=AlertStatus.NEW,
                             simulated_date=cutoff, evidence=candidate))
                db.flush()
                db.add(AuditLog(alert_id=alert_id, user_id=user_id, event_type="ALERT_DETECTED", payload={"test": True}))
                db.commit()
            app.dependency_overrides[get_db] = session
            headers = {"X-User-Id": str(user_id)}
            policy = PolicyReference(document_name="fixture.pdf", page_number=1, chunk_index=0, content="Cobertura mínima por clase.")
            with TestClient(app) as api, patch("app.analyst.service.OpenAI", return_value=client), \
                    patch("app.strategist.service.create_client", return_value=client), \
                    patch("app.analyst.service.retrieve_policies", return_value=[policy]), \
                    patch.object(settings, "openai_api_key", SecretStr("test-key")), \
                    patch.object(settings, "openai_model_reasoning", "test-model"):
                def post(suffix, body=None):
                    response = api.post(f"/alertas/{alert_id}/{suffix}", headers=headers, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()
                analysis = post("analizar")
                self.assertEqual(analysis["status"], "ANALYZING")
                self.assertEqual(analysis["root_cause"]["evidence"]["sku"], candidate["sku"])
                self.assertIsNone(analysis["proposals"])
                strategy = post("estrategia")
                self.assertEqual((strategy["status"], strategy["amount_at_risk"]), ("PROPOSED", None))
                post("decision", {"decision": "APPROVED"})
                result = post("ejecutar")
                self.assertEqual(result["alert_status"], "EXECUTED")
                self.assertEqual(len(result["actions"]), 3)
                self.assertTrue(all(a["result"]["sku"] == candidate["sku"] and a["result"]["sandbox"] for a in result["actions"]))
                self.assertEqual(api.post(f"/alertas/{alert_id}/ejecutar", headers=headers).status_code, 409)
                events = api.get("/bitacora", params={"alert_id": str(alert_id)}, headers=headers).json()
                self.assertEqual({e["event_type"] for e in events}, {
                    "ALERT_DETECTED", "ANALYSIS_STARTED", "ANALYSIS_COMPLETED", "STRATEGY_STARTED", "STRATEGY_COMPLETED",
                    "ALERT_DECISION_APPROVED", "EXECUTION_STARTED", "EXECUTION_COMPLETED"})
                self.assertEqual(client.responses.parse.call_count, 2)
        finally:
            app.dependency_overrides.pop(get_db, None)
            try:
                with Session(db_engine) as db:
                    for model in (ExecutionAction, AuditLog, Decision):
                        db.execute(delete(model).where(model.alert_id == alert_id))
                    db.execute(delete(Alert).where(Alert.id == alert_id))
                    db.execute(delete(User).where(User.id == user_id))
                    db.commit()
                    for model in (ExecutionAction, AuditLog, Decision):
                        self.assertEqual(db.scalar(select(func.count()).select_from(model).where(model.alert_id == alert_id)), 0)
                    self.assertEqual(db.scalar(select(func.count()).select_from(Alert).where(Alert.id == alert_id)), 0)
                    self.assertEqual(db.scalar(select(func.count()).select_from(User).where(User.id == user_id)), 0)
            finally:
                db_engine.dispose()


if __name__ == "__main__":
    unittest.main()
