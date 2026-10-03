import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from app.analyst.margin import investigate_margin, percent_change
from app.analyst.schemas import AnalysisExplanation, AnalysisFact, PolicyFinding
from app.rag.service import PolicyReference
from app.analyst.service import DEADLINE_LIMITATION, explain_margin
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AlertStatus, AuditLog, Base, Role, User


@compiles(JSONB, "sqlite")
def jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@compiles(CreateTable, "sqlite")
def defaults_sqlite(element, compiler, **kwargs):
    return compiler.visit_create_table(element, **kwargs).replace("'{}'::jsonb", "'{}'")


class AnalystTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cutoff = date(2026, 8, 20)
        cls.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

        @event.listens_for(cls.engine, "connect")
        def prepare(connection, record):
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("ATTACH DATABASE ':memory:' AS app")
            connection.execute("ATTACH DATABASE ':memory:' AS centinela")
            connection.create_function("gen_random_uuid", 0, lambda: uuid4().hex)
            connection.create_function("fecha_corte", 0, lambda: cls.cutoff.isoformat())

        @event.listens_for(cls.engine, "before_cursor_execute", retval=True)
        def syntax(connection, cursor, statement, parameters, context, executemany):
            if statement == "SET TRANSACTION READ ONLY":
                statement = "SELECT 1"  # SQLite no soporta este comando PostgreSQL.
            return statement.replace("centinela.fecha_corte()", "fecha_corte()"), parameters

        Base.metadata.create_all(cls.engine)
        with cls.engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE centinela.v_ventas (sku TEXT, fecha DATE, cantidad NUMERIC, valor_neto NUMERIC, costo_total NUMERIC, linea TEXT, producto TEXT)")
            db.exec_driver_sql("CREATE TABLE centinela.ref_margen_minimo_linea (linea TEXT, margen_minimo_pct NUMERIC)")
            db.exec_driver_sql("CREATE TABLE centinela.v_margen_semanal_linea (semana DATE, linea TEXT, margen_pct NUMERIC)")
            db.exec_driver_sql("CREATE TABLE centinela.costos_proveedor (sku TEXT, proveedor_id TEXT, fecha_vigencia DATE, costo_unitario NUMERIC)")
            db.exec_driver_sql("CREATE TABLE centinela.proveedores (proveedor_id TEXT, nombre TEXT)")
            db.exec_driver_sql("CREATE TABLE centinela.productos (sku TEXT, linea TEXT)")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.connection.exec_driver_sql("BEGIN")
        self.db = Session(bind=self.connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
        self.line = "Línea " + uuid4().hex
        self.db.execute(text("INSERT INTO centinela.ref_margen_minimo_linea VALUES (:line,25)"), {"line": self.line})
        self.week = self.cutoff - timedelta(days=self.cutoff.weekday())
        self.main_sku, self.other_sku = uuid4().hex[:6], uuid4().hex[:6]
        self.provider = uuid4().hex[:4]
        self.users = {}
        for role in Role:
            user = User(name=role.value, email=f"{uuid4()}@test.invalid", role=role, active=True)
            self.db.add(user)
            self.users[role] = user
        self.alert = Alert(type="MARGIN_ANOMALY", area="COMERCIAL", title="Fixture", summary="Prueba", severity="HIGH", status=AlertStatus.NEW,
                           simulated_date=self.cutoff, evidence={"linea": self.line, "semana": self.week.isoformat()})
        self.db.add(self.alert)
        self.db.flush()
        self.alert_id = self.alert.id
        for sku in [self.main_sku, self.other_sku]:
            self.db.execute(text("INSERT INTO centinela.productos VALUES (:sku,:line)"), {"sku": sku, "line": self.line})
        self.db.execute(text("INSERT INTO centinela.proveedores VALUES (:id,'Proveedor fixture')"), {"id": self.provider})
        for index in range(1, 10):
            previous = self.week - timedelta(weeks=index)
            self.db.execute(text("INSERT INTO centinela.v_margen_semanal_linea VALUES (:week,:line,:margin)"),
                            {"week": previous.isoformat(), "line": self.line, "margin": 30 if index <= 8 else 90})
            for sku in [self.main_sku, self.other_sku]:
                self.sale(sku, previous, 1, 100, 70 if index <= 8 else 10)
        self.sale(self.main_sku, self.week, 2, 200, 170)
        self.sale(self.other_sku, self.week, 1, 100, 75)
        self.sale(self.main_sku, self.cutoff + timedelta(days=1), 1, 1, 1000)
        for day, cost in [(self.week-timedelta(weeks=4), 70), (self.week, 85), (self.cutoff+timedelta(days=1), 1000)]:
            self.db.execute(text("INSERT INTO centinela.costos_proveedor VALUES (:sku,:provider,:day,:cost)"),
                            {"sku": self.main_sku, "provider": self.provider, "day": day.isoformat(), "cost": cost})
        self.db.commit()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.model_patch = patch.object(settings, "openai_model_reasoning", "test-model")
        self.model_patch.start()
        self.openai = MagicMock()
        self.policies = [PolicyReference(document_name="politica_fixture.pdf", page_number=1, chunk_index=0,
                                        content="El precio debe revisarse ante aumentos de costo en un máximo de 10 días hábiles.")]
        self.rag_patch = patch("app.analyst.service.retrieve_policies", return_value=self.policies)
        self.rag_patch.start()
        self.openai.responses.parse.return_value.status = "completed"
        self.openai.responses.parse.return_value.output_parsed = AnalysisExplanation(
            summary="El costo aumentó sin ajuste de precio.", root_cause="La presión de costos está asociada al menor margen observado.",
            facts=[AnalysisFact(statement="El precio ponderado se mantuvo mientras el costo creció.", source="DATA")], interpretation="Es una asociación observada, no prueba causal del proveedor.",
            policy_findings=[PolicyFinding(statement=self.policies[0].content, document_name=self.policies[0].document_name, page_number=1, chunk_index=0)],
            confidence=0.8, insufficient_evidence=True,
        )

    def tearDown(self):
        self.rag_patch.stop()
        self.model_patch.stop()
        self.client.close()
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def sale(self, sku, day, units, sales, costs):
        self.db.execute(text("INSERT INTO centinela.v_ventas VALUES (:sku,:day,:units,:sales,:costs,:line,'Producto fixture')"),
                        {"sku": sku, "day": day.isoformat(), "units": units, "sales": sales, "costs": costs, "line": self.line})

    def request(self, role=Role.GERENTE):
        with patch("app.analyst.service.OpenAI", return_value=self.openai), patch.object(settings, "openai_api_key", SecretStr("test-key")):
            return self.client.post(f"/alertas/{self.alert_id}/analizar", headers={"X-User-Id": str(self.users[role].id)})

    def logs(self):
        return self.db.scalars(select(AuditLog).where(AuditLog.alert_id == self.alert_id).order_by(AuditLog.created_at)).all()

    def test_sql_identifies_sku_and_computes_costs_prices_and_history(self):
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        self.assertEqual(len(evidence.semanas_historicas), 8)
        self.assertEqual(evidence.margen_historico_pct, Decimal("30"))
        self.assertEqual(evidence.margen_actual_pct, Decimal("18.33"))
        self.assertEqual(evidence.margen_minimo_pct, Decimal("25"))
        sku = evidence.sku_afectados[0]
        self.assertEqual(sku.sku, self.main_sku)
        self.assertEqual(sku.producto, "Producto fixture")
        self.assertEqual((sku.costo_anterior, sku.costo_actual), (Decimal("70"), Decimal("85")))
        self.assertEqual(sku.variacion_costo_pct, Decimal("1500") / Decimal("70"))
        self.assertEqual((sku.precio_anterior, sku.precio_actual), (Decimal("100"), Decimal("100")))
        self.assertFalse(sku.ajuste_precio)
        self.assertFalse(sku.ajuste_precio_cubre_costo)
        self.assertEqual(sku.contribucion_perdida_margen, Decimal("30"))
        self.assertEqual(sku.proveedores[0].proveedor_id, self.provider)
        self.assertEqual(sku.proveedores[0].costo_actual, Decimal("85"))
        self.assertEqual(sku.proveedores[0].costo_anterior, Decimal("70"))

    def test_price_increase_that_covers_cost_is_identified(self):
        self.db.execute(text("UPDATE centinela.v_ventas SET valor_neto=250 WHERE sku=:sku AND fecha=:day"), {"sku": self.main_sku, "day": self.week.isoformat()})
        sku = investigate_margin(self.db, self.line, self.week, self.cutoff).sku_afectados
        sku = next(value for value in sku if value.sku == self.main_sku)
        self.assertEqual(sku.variacion_precio_pct, Decimal("25"))
        self.assertTrue(sku.ajuste_precio_cubre_costo)

    def test_success_states_root_cause_confidence_and_audit(self):
        def parse(**kwargs):
            self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.ANALYZING)
            self.assertIn("investigation", self.db.get(Alert, self.alert_id).evidence)
            self.assertEqual([event.event_type for event in self.logs()], ["ANALYSIS_STARTED"])
            self.assertEqual(kwargs["model"], "test-model")
            self.assertFalse(kwargs["store"])
            self.assertEqual(kwargs["text_format"], AnalysisExplanation)
            self.assertNotIn("test-key", kwargs["input"])
            self.assertNotIn("DATABASE_URL", kwargs["input"])
            self.assertIn("UNTRUSTED DATA", kwargs["instructions"])
            self.assertIn("policies_untrusted", kwargs["input"])
            self.assertIn("business_day_deadline_evaluated", kwargs["input"])
            return self.openai.responses.parse.return_value
        self.openai.responses.parse.side_effect = parse
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.ANALYZING)
        self.assertEqual(alert.root_cause["analysis"]["summary"], "El costo aumentó sin ajuste de precio.")
        self.assertEqual(alert.root_cause["policy_references"][0]["document_name"], self.policies[0].document_name)
        self.assertEqual(alert.root_cause["evidence"]["sku_afectados"][0]["sku"], self.main_sku)
        self.assertAlmostEqual(float(alert.confidence), .8)
        self.assertIsNone(alert.proposals)
        self.assertEqual([event.event_type for event in self.logs()], ["ANALYSIS_STARTED", "ANALYSIS_COMPLETED"])

    def test_openai_failure_preserves_evidence_and_hides_secrets(self):
        self.openai.responses.parse.side_effect = RuntimeError("secret-key DATABASE_URL")
        with self.assertLogs("app.analyst.service", level="ERROR") as logs:
            response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-key", response.text + str(logs.output))
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.FAILED)
        self.assertIn("investigation", alert.evidence)
        self.assertIsNone(alert.root_cause)
        self.assertEqual([event.event_type for event in self.logs()], ["ANALYSIS_STARTED", "ANALYSIS_FAILED"])

    def test_roles_and_duplicate_analysis(self):
        for role in [Role.AUDITOR, Role.LIDER_PROCESO]:
            self.assertEqual(self.request(role).status_code, 403)
        self.assertEqual(self.request(Role.ANALISTA).status_code, 200)
        self.assertEqual(self.request().status_code, 409)
        self.openai.responses.parse.assert_called_once()

    def test_wrong_type_and_invalid_week_are_rejected(self):
        self.alert.type = "OTHER"
        self.db.commit()
        self.assertEqual(self.request().status_code, 400)
        self.alert.type = "MARGIN_ANOMALY"
        self.alert.evidence = {"linea": self.line, "semana": "invalid"}
        self.db.commit()
        self.assertEqual(self.request().status_code, 422)
        self.openai.responses.parse.assert_not_called()

    def test_structured_output_rejects_invented_numbers_and_incomplete_response(self):
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        self.openai.responses.parse.return_value.output_parsed.summary = "La caída fue de 99 puntos."
        with self.assertRaises(ValueError):
            explain_margin(evidence, self.openai, self.policies)
        self.openai.responses.parse.return_value.output_parsed = None
        with self.assertRaises(ValueError):
            explain_margin(evidence, self.openai, self.policies)

    def test_absent_history_has_no_invented_variations(self):
        self.db.execute(text("DELETE FROM centinela.v_margen_semanal_linea"))
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        self.assertIsNone(evidence.margen_historico_pct)
        self.assertTrue(all(sku.variacion_costo_pct is None for sku in evidence.sku_afectados))
        self.openai.responses.parse.return_value.output_parsed.facts = [AnalysisFact(statement="No hay margen histórico comparable de la línea.", source="DATA")]
        self.assertTrue(explain_margin(evidence, self.openai, self.policies).insufficient_evidence)
        self.assertIsNone(percent_change(Decimal("0"), Decimal("10")))

    def test_missing_unknown_and_inactive_user(self):
        path = f"/alertas/{self.alert_id}/analizar"
        self.assertEqual(self.client.post(path).status_code, 401)
        for value in ["invalid", str(uuid4())]:
            self.assertEqual(self.client.post(path, headers={"X-User-Id": value}).status_code, 401)
        self.users[Role.GERENTE].active = False
        self.db.commit()
        self.assertEqual(self.request().status_code, 403)
        self.openai.responses.parse.assert_not_called()

    def test_all_non_new_states_are_rejected(self):
        for state in AlertStatus:
            if state == AlertStatus.NEW:
                continue
            self.alert.status = state
            self.db.commit()
            self.assertEqual(self.request().status_code, 409)
        self.openai.responses.parse.assert_not_called()

    def test_completion_audit_failure_rolls_back_result_but_preserves_evidence(self):
        real_flush = self.db.flush

        def flush(*args, **kwargs):
            if any(isinstance(row, AuditLog) and row.event_type == "ANALYSIS_COMPLETED" for row in self.db.new):
                raise RuntimeError("secret-error")
            return real_flush(*args, **kwargs)

        with patch.object(self.db, "flush", side_effect=flush), self.assertLogs("app.analyst.service", level="ERROR"):
            response = self.request()
        self.assertEqual(response.status_code, 503)
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.FAILED)
        self.assertIsNone(alert.root_cause)
        self.assertIsNone(alert.confidence)
        self.assertIn("investigation", alert.evidence)
        self.assertNotIn("ANALYSIS_COMPLETED", [row.event_type for row in self.logs()])

    def test_model_cannot_add_unverified_facts(self):
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        self.openai.responses.parse.return_value.output_parsed.facts = [AnalysisFact(statement="El proveedor sufrió una huelga.", source="DATA")]
        with self.assertRaises(ValueError):
            explain_margin(evidence, self.openai, self.policies)

    def test_document_reference_and_deadline_claim_are_rejected(self):
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        output = self.openai.responses.parse.return_value.output_parsed
        output.policy_findings[0].page_number = 99
        with self.assertRaises(ValueError):
            explain_margin(evidence, self.openai, self.policies)
        output.policy_findings[0].page_number = 1
        output.summary = "Se incumplió el plazo de revisión."
        result = explain_margin(evidence, self.openai, self.policies)
        self.assertEqual(result.summary, DEADLINE_LIMITATION)

    def test_deadline_disclaimers_do_not_fail_valid_analysis(self):
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        output = self.openai.responses.parse.return_value.output_parsed
        for disclaimer in [
            "No se puede afirmar incumplimiento del plazo sin calendario oficial.",
            "No se ha demostrado que el plazo esté vencido.",
            "No se confirma incumplimiento; sin embargo, se incumplió el plazo.",
        ]:
            output.interpretation = "El costo aumentó sin ajuste proporcional de precio. " + disclaimer
            result = explain_margin(evidence, self.openai, self.policies)
            self.assertIn("El costo aumentó sin ajuste proporcional de precio.", result.interpretation)
            self.assertIn(DEADLINE_LIMITATION, result.interpretation)
            self.assertNotIn("incumpl", result.interpretation)
            self.assertEqual(result.policy_findings[0].statement, self.policies[0].content)

    def test_rag_failure_preserves_sql_evidence(self):
        with patch("app.analyst.service.retrieve_policies", side_effect=RuntimeError("secret")), self.assertLogs("app.analyst.service", level="ERROR"):
            response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertIn("investigation", self.db.get(Alert, self.alert_id).evidence)
        self.openai.responses.parse.assert_not_called()
        self.assertEqual(self.logs()[-1].payload["stage"], "policy_retrieval")

    def test_policy_quotes_may_contain_numbers_but_must_be_verbatim(self):
        evidence = investigate_margin(self.db, self.line, self.week, self.cutoff)
        output = self.openai.responses.parse.return_value.output_parsed
        output.facts.append(AnalysisFact(statement=self.policies[0].content, source="POLICY"))
        self.assertEqual(len(explain_margin(evidence, self.openai, self.policies).facts), 2)
        output.facts[-1].statement = "Una regla inventada exige una revisión."
        with self.assertRaises(ValueError):
            explain_margin(evidence, self.openai, self.policies)


if __name__ == "__main__":
    unittest.main()
