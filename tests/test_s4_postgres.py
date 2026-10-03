"""S4 end-to-end sobre PostgreSQL oficial; OpenAI mockeado y rollback total."""

import os
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.analyst.schemas import AnalysisExplanation, AnalysisFact
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AlertStatus, AuditLog, Role, User
from app.rag.service import PolicyReference
from app.strategist.schemas import ActionType, ModelProposal, ModelStrategy
from app.vigil.detectors.discount import DiscountDetector
from app.vigil.service import run_vigil


@unittest.skipUnless(
    os.environ.get("CENTINELA_TEST_POSTGRES") == "1",
    "Requiere CENTINELA_TEST_POSTGRES=1",
)
class S4PostgresTests(unittest.TestCase):
    def test_official_dataset_complete_mocked_lifecycle_and_rollback(self):
        root = Path(__file__).resolve().parents[1]
        url = make_url(dotenv_values(root / ".env")["DATABASE_URL_UNPOOLED"]).set(
            drivername="postgresql+psycopg"
        )
        engine = create_engine(url, connect_args={"connect_timeout": 15})
        connection = engine.connect()
        transaction = connection.begin()
        user_id = uuid4()
        try:
            with connection.connection.driver_connection.cursor() as cursor:
                for name in (
                    "04_reloj_simulado.sql", "05_core_app.sql", "06_margin_detector.sql",
                    "08_executor.sql", "09_discount_scenario.sql",
                ):
                    cursor.execute((root / "database/sql" / name).read_text(encoding="utf-8"))
            db = Session(
                bind=connection, join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            db.execute(text(
                'UPDATE app.simulation_state SET "current_date"=DATE \'2026-09-30\' WHERE id=1'
            ))
            latest_week = db.scalar(text(
                "SELECT max(date_trunc('week', fecha)::date) "
                "FROM centinela.v_descuentos_fuera_politica"
            ))
            self.assertIsNotNone(latest_week, "El dataset oficial debe contener violaciones S4")
            cutoff = min(latest_week + timedelta(days=6), date(2026, 9, 30))
            db.execute(text(
                'UPDATE app.simulation_state SET "current_date"=:cutoff WHERE id=1'
            ), {"cutoff": cutoff})
            db.add(User(
                id=user_id, name="Fixture S4", email=f"{user_id}@test.invalid",
                role=Role.GERENTE, active=True,
            ))
            db.commit()

            before = set(db.scalars(select(Alert.id)).all())
            result = run_vigil(db, user_id, detectors=[DiscountDetector()])
            self.assertEqual(result.errores, [])
            alerts = db.scalars(select(Alert).where(
                Alert.id.not_in(before), Alert.type == "DISCOUNT_POLICY_VIOLATION",
            )).all()
            self.assertEqual(result.alertas_nuevas, len(alerts))
            self.assertTrue(alerts, "La última semana oficial con violaciones debe generar S4")
            alert = alerts[0]
            self.assertEqual(alert.simulated_date, cutoff)
            self.assertEqual(alert.evidence["semana"], latest_week.isoformat())
            self.assertGreater(alert.evidence["cantidad_lineas_fuera_politica"], 0)
            self.assertEqual(run_vigil(
                db, user_id, detectors=[DiscountDetector()],
            ).alertas_nuevas, 0)

            def session():
                yield db

            app.dependency_overrides[get_db] = session
            model = MagicMock()
            model.responses.parse.return_value.status = "completed"
            model.responses.parse.side_effect = [
                type("Response", (), {
                    "status": "completed",
                    "output_parsed": AnalysisExplanation(
                        summary="Se observaron descuentos fuera de política.",
                        root_cause="El patrón corresponde a descuentos sin aprobación especial.",
                        facts=[AnalysisFact(
                            statement="Hay líneas con descuento superior al tope normal sin aprobación especial.",
                            source="DATA",
                        )],
                        policy_findings=[], interpretation="La evidencia requiere revisión humana.",
                        confidence=.9, insufficient_evidence=False,
                    ),
                })(),
                type("Response", (), {
                    "status": "completed",
                    "output_parsed": ModelStrategy(
                        summary="Revisión comercial respaldada por evidencia",
                        proposals=[ModelProposal(
                            id="discount-review",
                            action_type=ActionType.CREATE_DISCOUNT_REVIEW_TASK,
                            title="Revisión comercial",
                            description="Revisar la evidencia con el responsable comercial.",
                            reason="Se observaron descuentos fuera de política.",
                            priority="HIGH",
                        )],
                    ),
                })(),
            ]
            policy = PolicyReference(
                document_name="fixture.pdf", page_number=1, chunk_index=0,
                content="Los descuentos requieren aprobación especial.",
            )
            headers = {"X-User-Id": str(user_id)}
            with TestClient(app) as api, patch(
                "app.analyst.service.OpenAI", return_value=model,
            ), patch(
                "app.strategist.service.create_client", return_value=model,
            ), patch(
                "app.analyst.service.retrieve_policies", return_value=[policy],
            ), patch.object(
                settings, "openai_api_key", SecretStr("test-key"),
            ), patch.object(settings, "openai_model_reasoning", "test-model"):
                def post(suffix, body=None):
                    response = api.post(
                        f"/alertas/{alert.id}/{suffix}", headers=headers, json=body,
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()

                self.assertEqual(post("analizar")["status"], "ANALYZING")
                strategy = post("estrategia")
                self.assertEqual(strategy["status"], "PROPOSED")
                self.assertEqual(
                    Decimal(strategy["amount_at_risk"]),
                    Decimal(str(alert.evidence["descuento_exceso_total"])),
                )
                post("decision", {"decision": "APPROVED"})
                execution = post("ejecutar")
                self.assertEqual(execution["alert_status"], "EXECUTED")
                self.assertEqual(execution["actions"][0]["result"]["status"], "TASK_CREATED")

            events = set(db.scalars(select(AuditLog.event_type).where(
                AuditLog.alert_id == alert.id,
            )).all())
            self.assertEqual(events, {
                "ALERT_DETECTED", "ANALYSIS_STARTED", "ANALYSIS_COMPLETED",
                "STRATEGY_STARTED", "STRATEGY_COMPLETED",
                "ALERT_DECISION_APPROVED", "EXECUTION_STARTED", "EXECUTION_COMPLETED",
            })
            db.close()
        finally:
            app.dependency_overrides.pop(get_db, None)
            transaction.rollback()
            connection.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
