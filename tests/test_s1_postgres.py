"""S1 sobre PostgreSQL real y datos oficiales; OpenAI mockeado, limpieza por UUID propio."""
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

from app.analyst.schemas import AnalysisExplanation, AnalysisFact
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AlertStatus, Area, AuditLog, Decision, ExecutionAction, Role, User
from app.rag.service import PolicyReference
from app.vigil.detectors.margin import MARGIN_QUERY, margin_evidence
import test_s1 as s1_fixtures


@unittest.skipUnless(os.environ.get("CENTINELA_TEST_POSTGRES") == "1", "Requiere CENTINELA_TEST_POSTGRES=1")
class S1PostgresTests(unittest.TestCase):
    def test_new_analysis_strategy_decision_execution_and_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        url = make_url(dotenv_values(root / ".env")["DATABASE_URL_UNPOOLED"]).set(drivername="postgresql+psycopg")
        engine = create_engine(url, connect_args={"connect_timeout": 15})
        alert_id, user_id = uuid4(), uuid4()
        def session():
            with Session(engine) as db:
                yield db
        client = MagicMock()
        client.responses.parse.return_value.status = "completed"
        client.responses.parse.side_effect = [
            type("Response", (), {"status":"completed", "output_parsed":AnalysisExplanation(
                summary="Análisis descriptivo", root_cause="La causa no está demostrada.",
                facts=[], interpretation="Requiere revisión humana de la evidencia.", confidence=.6, insufficient_evidence=True)})(),
            type("Response", (), {"status":"completed", "output_parsed":s1_fixtures.model_strategy()})(),
        ]
        try:
            with Session(engine) as db:
                cutoff = db.scalar(text("SELECT centinela.fecha_corte()"))
                week = cutoff-timedelta(days=cutoff.weekday())
                rows = db.execute(MARGIN_QUERY, {"week":week}).mappings().all()
                candidate = next((margin_evidence(r) for r in rows if margin_evidence(r)), None)
                self.assertIsNotNone(candidate, "El reloj actual debe disponer de una anomalía real de margen")
                db.add(User(id=user_id, name="Fixture S1", email=f"{user_id}@test.invalid", role=Role.GERENTE, active=True))
                db.add(Alert(id=alert_id, type="MARGIN_ANOMALY", area=Area.COMERCIAL, title="Fixture S1",
                             summary="Fixture basada en métrica oficial", severity="HIGH", status=AlertStatus.NEW,
                             simulated_date=cutoff, evidence=candidate))
                db.flush()
                db.add(AuditLog(alert_id=alert_id, user_id=user_id, event_type="ALERT_DETECTED", payload={"test":True}))
                db.commit()
            app.dependency_overrides[get_db] = session
            headers = {"X-User-Id":str(user_id)}
            policy = PolicyReference(document_name="fixture.pdf", page_number=1, chunk_index=0, content="Revisar precios ante cambios de costo.")
            with TestClient(app) as api, patch("app.analyst.service.OpenAI", return_value=client), patch("app.strategist.service.create_client", return_value=client), patch("app.analyst.service.retrieve_policies", return_value=[policy]), patch.object(settings,"openai_api_key",SecretStr("test-key")), patch.object(settings,"openai_model_reasoning","test-model"):
                def post(suffix, body=None):
                    response = api.post(f"/alertas/{alert_id}/{suffix}", headers=headers, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()
                analysis = post("analizar")
                self.assertEqual(analysis["status"], "ANALYZING")
                self.assertIsNotNone(analysis["root_cause"])
                self.assertIsNone(analysis["proposals"])
                strategy = post("estrategia")
                self.assertEqual(strategy["status"], "PROPOSED")
                self.assertEqual(api.post(f"/alertas/{alert_id}/estrategia", headers=headers).status_code, 409)
                post("decision", {"decision":"APPROVED"})
                result = post("ejecutar")
                self.assertEqual(result["alert_status"], "EXECUTED")
                self.assertEqual(len(result["actions"]), 3)
                self.assertEqual(api.post(f"/alertas/{alert_id}/ejecutar",headers=headers).status_code,409)
                self.assertEqual(len(api.get(f"/alertas/{alert_id}/ejecuciones",headers=headers).json()),3)
                events = api.get("/bitacora",params={"alert_id":str(alert_id)},headers=headers).json()
                self.assertEqual({e["event_type"] for e in events}, {
                    "ALERT_DETECTED", "ANALYSIS_STARTED", "ANALYSIS_COMPLETED", "STRATEGY_STARTED", "STRATEGY_COMPLETED",
                    "ALERT_DECISION_APPROVED", "EXECUTION_STARTED", "EXECUTION_COMPLETED"})
                self.assertEqual(client.responses.parse.call_count,2)
        finally:
            app.dependency_overrides.pop(get_db, None)
            try:
                with Session(engine) as db:
                    for model in (ExecutionAction, AuditLog, Decision):
                        db.execute(delete(model).where(model.alert_id == alert_id))
                    db.execute(delete(Alert).where(Alert.id == alert_id))
                    db.execute(delete(User).where(User.id == user_id))
                    db.commit()
                    for model in (ExecutionAction, AuditLog, Decision):
                        self.assertEqual(db.scalar(select(func.count()).select_from(model).where(model.alert_id==alert_id)),0)
                    self.assertEqual(db.scalar(select(func.count()).select_from(Alert).where(Alert.id==alert_id)),0)
                    self.assertEqual(db.scalar(select(func.count()).select_from(User).where(User.id==user_id)),0)
            finally:
                engine.dispose()
