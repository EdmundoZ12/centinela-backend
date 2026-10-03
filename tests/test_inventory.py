"""S3 Inventario sobre SQLite con OpenAI y RAG mockeados; IDs aleatorios por prueba."""
import re
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import test_analyst  # noqa: F401  Registra la compilación JSONB/CREATE TABLE para SQLite.
from app.analyst.inventory import investigate_inventory
from app.analyst.schemas import AnalysisExplanation, AnalysisFact, PolicyFinding
from app.analyst.service import explain_inventory, inventory_facts_catalog
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AlertStatus, Area, AuditLog, Base, ExecutionAction, Role, User
from app.rag.service import INVENTORY_POLICY_QUERY, PolicyReference
from app.strategist.schemas import ActionType, InventoryActionType, InventoryModelProposal, InventoryModelStrategy
from app.strategist.service import propose_strategy
from app.vigil.detectors.inventory import InventoryDetector
from app.vigil.service import DETECTORS, run_vigil

ROOT = Path(__file__).resolve().parents[1]
DATA_FACT = "La demanda promedio de los últimos treinta días es superior a la del periodo previo."
POLICY = "Si la cobertura es menor a 5 días y hay pedidos pendientes de despacho, el caso es crítico."


def inventory_strategy(count=3, actions=None):
    actions = actions or list(InventoryActionType)
    return InventoryModelStrategy(summary="Seguimiento interno de reposición", proposals=[
        InventoryModelProposal(id=f"proposal-{i}", action_type=actions[i], title="Revisión interna",
            description="Revisar la reposición del producto con Compras.",
            reason="La orden esperada no se ha recibido y la cobertura es crítica.", priority="HIGH")
        for i in range(count)
    ])


def explanation(**changes):
    values = dict(
        summary="La cobertura cayó con demanda creciente y reposición pendiente.",
        root_cause="La demanda creció mientras una orden de compra quedó retrasada.",
        facts=[AnalysisFact(statement=DATA_FACT, source="DATA"), AnalysisFact(statement=POLICY, source="POLICY")],
        policy_findings=[PolicyFinding(statement=POLICY, document_name="politica_fixture.pdf", page_number=1, chunk_index=0)],
        interpretation="Asociación observada; requiere confirmación de Compras.", confidence=0.8, insufficient_evidence=False)
    values.update(changes)
    return AnalysisExplanation(**values)


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cutoff = date(2026, 9, 16)
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
            return statement.replace("centinela.fecha_corte()", "fecha_corte()").replace(" FOR SHARE", ""), parameters

        Base.metadata.create_all(cls.engine)
        with cls.engine.begin() as db:
            for ddl in (
                'CREATE TABLE app.simulation_state (id INTEGER PRIMARY KEY, "current_date" DATE)',
                "INSERT INTO app.simulation_state VALUES (1, '2026-09-16')",
                "CREATE TABLE centinela.productos (sku TEXT, nombre TEXT, linea TEXT, proveedor_id TEXT, clase_abc TEXT)",
                "CREATE TABLE centinela.proveedores (proveedor_id TEXT, nombre TEXT, lead_time_dias INTEGER)",
                "CREATE TABLE centinela.inventario_diario (fecha DATE, bodega_id TEXT, sku TEXT, entradas INTEGER, salidas INTEGER, existencia_final INTEGER)",
                "CREATE TABLE centinela.ordenes_compra (oc_id TEXT, proveedor_id TEXT, sku TEXT, bodega_id TEXT, fecha_oc DATE, "
                "fecha_esperada DATE, fecha_recibida DATE, cantidad INTEGER, estado TEXT)",
                # La vista oficial se valida en PostgreSQL; aquí es una tabla con sus columnas.
                "CREATE TABLE centinela.v_cobertura_inventario (sku TEXT, nombre TEXT, linea TEXT, clase_abc TEXT, bodega_id TEXT, "
                "existencia INTEGER, demanda_prom_30d NUMERIC, cobertura_dias NUMERIC, unidades_pendientes NUMERIC)",
                # MarginDetector también corre en el Vigía; sin filas no genera alertas.
                "CREATE TABLE centinela.v_margen_semanal_linea (semana DATE, linea TEXT, margen_pct NUMERIC)",
                "CREATE TABLE centinela.ref_margen_minimo_linea (linea TEXT, margen_minimo_pct NUMERIC)",
            ):
                db.exec_driver_sql(ddl)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        type(self).cutoff = date(2026, 9, 16)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.connection.exec_driver_sql("BEGIN")
        self.db = Session(bind=self.connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
        self.sku, self.warehouse = "S" + uuid4().hex[:5], "B-" + uuid4().hex[:6]
        self.supplier, self.catalog_supplier = "V" + uuid4().hex[:3], "V" + uuid4().hex[:3]
        self.users = {}
        for role in Role:
            user = User(name=role.value, email=f"{uuid4()}@test.invalid", role=role, active=True,
                        area=Area.INVENTARIO if role == Role.LIDER_PROCESO else None)
            self.db.add(user)
            self.users[role] = user
        self.db.commit()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.patches = [patch.object(settings, "openai_model_reasoning", "test-model"),
                        patch.object(settings, "openai_api_key", SecretStr("test-key"))]
        self.openai = MagicMock()
        self.openai.responses.parse.return_value.status = "completed"
        self.openai.responses.parse.return_value.output_parsed = explanation()
        self.policies = [PolicyReference(document_name="politica_fixture.pdf", page_number=1, chunk_index=0, content=POLICY)]
        self.rag = MagicMock(return_value=self.policies)
        self.strategy_client = MagicMock()
        self.strategy_client.responses.parse.return_value.status = "completed"
        self.strategy_client.responses.parse.return_value.output_parsed = inventory_strategy()
        self.patches += [patch("app.analyst.service.OpenAI", return_value=self.openai),
                         patch("app.analyst.service.retrieve_policies", self.rag),
                         patch("app.strategist.service.create_client", return_value=self.strategy_client)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    # --- fixtures -------------------------------------------------------------------------
    def coverage_row(self, coverage, pending=0, abc="A", stock=60, demand=20, sku=None):
        self.db.execute(text("INSERT INTO centinela.v_cobertura_inventario VALUES (:sku,'Producto fixture','Línea fixture',:abc,:wh,:stock,:demand,:coverage,:pending)"),
                        {"sku": sku or self.sku, "abc": abc, "wh": self.warehouse, "stock": stock, "demand": demand, "coverage": coverage, "pending": pending})
        self.db.commit()

    def history(self, increase=True, late_order=True):
        """90 días: 60 previos con salidas 10/día, 30 actuales con 20 (o 10) y existencia 60 al corte."""
        self.db.execute(text("INSERT INTO centinela.productos VALUES (:sku,'Producto fixture','Línea fixture',:p,'A')"),
                        {"sku": self.sku, "p": self.catalog_supplier})
        for provider, name in ((self.supplier, "Proveedor de la orden"), (self.catalog_supplier, "Proveedor de catálogo")):
            self.db.execute(text("INSERT INTO centinela.proveedores VALUES (:p,:n,8)"), {"p": provider, "n": name})
        for offset in range(-89, 6):
            day = self.cutoff + timedelta(days=offset)
            outflow = 999 if offset > 0 else (20 if increase and offset > -30 else 10)
            self.db.execute(text("INSERT INTO centinela.inventario_diario VALUES (:d,:wh,:sku,:e,:s,:f)"), {
                "d": day.isoformat(), "wh": self.warehouse, "sku": self.sku, "e": 500 if offset == -42 else 0,
                "s": outflow, "f": 60 if offset == 0 else 600})
        orders = [("OC-R" + uuid4().hex[:4], -50, -42, -42, 500)]
        if late_order:
            # El estado final dice recibida, pero la recepción es posterior al corte: al corte está retrasada.
            orders.append(("OC-L" + uuid4().hex[:4], -20, -12, 4, 700))
        orders += [("OC-T" + uuid4().hex[:4], -5, 2, 3, 300), ("OC-F" + uuid4().hex[:4], 1, 8, None, 900)]
        for oc, ordered, expected, received, qty in orders:
            self.db.execute(text("INSERT INTO centinela.ordenes_compra VALUES (:oc,:p,:sku,:wh,:o,:e,:r,:q,'Recibida')"), {
                "oc": oc, "p": self.supplier, "sku": self.sku, "wh": self.warehouse,
                "o": (self.cutoff + timedelta(days=ordered)).isoformat(), "e": (self.cutoff + timedelta(days=expected)).isoformat(),
                "r": (self.cutoff + timedelta(days=received)).isoformat() if received is not None else None, "q": qty})
        self.db.commit()

    def detect(self, **row):
        self.coverage_row(**({"coverage": 3.0, "pending": 15} | row))
        self.assertEqual(InventoryDetector().run(self.db), 1)
        self.db.commit()
        self.alert = self.db.scalar(select(Alert).where(Alert.type == "INVENTORY_RISK"))
        return self.alert

    def post(self, suffix, role=Role.GERENTE, body=None):
        return self.client.post(f"/alertas/{self.alert.id}/{suffix}", json=body, headers={"X-User-Id": str(self.users[role].id)})

    def logs(self):
        return self.db.scalars(select(AuditLog).where(AuditLog.alert_id == self.alert.id).order_by(AuditLog.created_at)).all()

    def events(self):
        return [e.event_type for e in self.logs()]

    def analyzed(self):
        self.history()
        self.detect()
        response = self.post("analizar")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def proposed(self):
        self.analyzed()
        response = self.post("estrategia")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def approved(self):
        self.proposed()
        self.assertEqual(self.post("decision", body={"decision": "APPROVED"}).status_code, 200)

    def actions(self):
        return self.db.scalars(select(ExecutionAction).where(ExecutionAction.alert_id == self.alert.id)).all()

    # --- detector -------------------------------------------------------------------------
    def test_sufficient_class_a_coverage_creates_no_alert(self):
        self.coverage_row(10.0)
        self.coverage_row(25.0, pending=50, sku="S" + uuid4().hex[:5])
        self.assertEqual(InventoryDetector().run(self.db), 0)

    def test_class_a_below_ten_days_creates_high_alert_with_real_evidence(self):
        alert = self.detect(coverage=9.9, pending=0, stock=198, demand=20)
        self.assertEqual((alert.type, alert.area, alert.status, alert.severity), ("INVENTORY_RISK", Area.INVENTARIO, AlertStatus.NEW, "HIGH"))
        self.assertEqual(alert.simulated_date, self.cutoff)
        self.assertEqual(alert.evidence, {
            "sku": self.sku, "producto": "Producto fixture", "linea": "Línea fixture", "clase_abc": "A",
            "bodega_id": self.warehouse, "fecha_corte": self.cutoff.isoformat(), "existencia": 198,
            "demanda_prom_30d": 20.0, "cobertura_dias": 9.9, "cobertura_minima_dias": 10.0,
            "unidades_pendientes": 0.0, "trigger_cobertura": True, "trigger_critico": False})
        self.assertTrue(all(v is None for v in (alert.root_cause, alert.proposals, alert.confidence, alert.amount_at_risk)))

    def test_critical_requires_low_coverage_and_pending_orders(self):
        self.assertEqual(self.detect(coverage=4.9, pending=3).severity, "CRITICAL")
        self.assertTrue(self.alert.evidence["trigger_critico"])
        self.db.execute(text("DELETE FROM centinela.v_cobertura_inventario"))
        self.sku = "S" + uuid4().hex[:5]
        self.coverage_row(4.9, pending=0)
        self.assertEqual(InventoryDetector().run(self.db), 1)
        alert = next(a for a in self.db.scalars(select(Alert)).all() if a.evidence.get("sku") == self.sku)
        self.assertEqual((alert.severity, alert.evidence["trigger_critico"]), ("HIGH", False))

    def test_class_b_and_c_minimums(self):
        for abc, below, at in (("B", 6.9, 7.0), ("C", 4.9, 5.0)):
            sku_below, sku_at = "S" + uuid4().hex[:5], "S" + uuid4().hex[:5]
            self.coverage_row(below, abc=abc, sku=sku_below)
            self.coverage_row(at, abc=abc, sku=sku_at)
        self.assertEqual(InventoryDetector().run(self.db), 2)
        alerts = self.db.scalars(select(Alert).where(Alert.type == "INVENTORY_RISK")).all()
        self.assertEqual(sorted(a.evidence["cobertura_minima_dias"] for a in alerts), [5.0, 7.0])

    def test_rerun_is_idempotent_and_logs_single_detection(self):
        self.detect()
        self.assertEqual(InventoryDetector().run(self.db), 0)
        self.assertEqual(len(self.db.scalars(select(Alert).where(Alert.type == "INVENTORY_RISK")).all()), 1)
        self.assertEqual(self.events(), ["ALERT_DETECTED"])
        self.assertEqual(self.logs()[0].payload["evidence"], self.alert.evidence)

    def test_new_week_or_escalation_creates_distinct_alert(self):
        self.detect(coverage=8.0, pending=0)
        self.db.execute(text("UPDATE centinela.v_cobertura_inventario SET cobertura_dias=3, unidades_pendientes=4"))
        self.assertEqual(InventoryDetector().run(self.db), 1)
        type(self).cutoff += timedelta(weeks=1)
        self.assertEqual(InventoryDetector().run(self.db), 1)
        keys = self.db.scalars(select(Alert.dedupe_key).where(Alert.type == "INVENTORY_RISK")).all()
        self.assertEqual(len(set(keys)), 3)

    def test_zero_demand_without_coverage_is_skipped(self):
        self.coverage_row(None, pending=10, stock=0, demand=0)
        self.assertEqual(InventoryDetector().run(self.db), 0)

    def test_vigil_runs_inventory_detector(self):
        self.assertIn("inventory_detector", [d.name for d in DETECTORS])
        self.coverage_row(2.0, pending=5)
        result = run_vigil(self.db)
        self.assertEqual((result.detectores_ejecutados, result.alertas_nuevas, result.errores), (len(DETECTORS), 1, []))

    def test_s3_sources_do_not_hardcode_dataset_entities(self):
        pattern = re.compile(r"\bP\d{4}\b|BOD-[A-Z]{3}|OC-\d{6}|\bPR\d{2}\b|2026-\d{2}-\d{2}")
        for path in ("app/vigil/detectors/inventory.py", "app/analyst/inventory.py", "app/analyst/service.py",
                     "app/strategist/service.py", "app/executor/service.py"):
            self.assertIsNone(pattern.search((ROOT / path).read_text(encoding="utf-8")), path)

    # --- analista -------------------------------------------------------------------------
    def test_investigation_uses_sql_until_alert_cutoff(self):
        self.history()
        alert = self.detect()
        evidence = investigate_inventory(self.db, alert.evidence, self.cutoff)
        self.assertEqual(evidence.inventory.existencia, 60)
        self.assertEqual(evidence.inventory.cobertura_dias, Decimal("3.0"))
        self.assertTrue(evidence.cobertura_consistente_con_detector)
        demand = evidence.demand_analysis
        self.assertEqual((demand.dias_actuales, demand.dias_previos), (30, 60))
        self.assertEqual((demand.demanda_actual, demand.demanda_historica, demand.variacion_demanda_pct), (20, 10, 100))
        self.assertEqual(demand.entradas_ventana_actual, 0)
        self.assertEqual(demand.salidas_ventana_actual, 600)
        self.assertTrue(evidence.aumento_demanda)
        self.assertTrue(all(day.fecha <= self.cutoff for day in evidence.serie_reciente))
        states = {o.orden_id[:4]: o for o in evidence.purchase_orders}
        self.assertNotIn("OC-F", states, "Una orden emitida después del corte es dato futuro.")
        self.assertEqual((states["OC-R"].estado_al_corte, states["OC-R"].dias_retraso), ("RECIBIDA", 0))
        self.assertEqual((states["OC-L"].estado_al_corte, states["OC-L"].dias_retraso, states["OC-L"].fecha_recibida), ("RETRASADA", 12, None))
        self.assertEqual((states["OC-T"].estado_al_corte, states["OC-T"].fecha_recibida), ("EN_TRANSITO", None))
        self.assertEqual((states["OC-L"].proveedor_id, states["OC-L"].proveedor), (self.supplier, "Proveedor de la orden"))
        self.assertEqual((evidence.ordenes_retrasadas, evidence.unidades_retrasadas), (1, 700))
        self.assertEqual(evidence.proveedor_catalogo.proveedor_id, self.catalog_supplier)
        self.assertIn("no se puede demostrar una entrega parcial", " ".join(evidence.limitaciones))

    def test_analysis_moves_to_analyzing_and_persists_cause(self):
        body = self.analyzed().json()
        alert = self.db.get(Alert, self.alert.id)
        self.assertEqual((body["status"], alert.status), ("ANALYZING", AlertStatus.ANALYZING))
        self.assertIsNone(alert.proposals)
        self.assertEqual(alert.confidence, Decimal("0.8"))
        self.assertFalse(alert.root_cause["analysis"]["insufficient_evidence"])
        self.assertEqual(alert.root_cause["analysis"]["root_cause"], "La demanda creció mientras una orden de compra quedó retrasada.")
        self.assertEqual(alert.root_cause["evidence"]["sku"], self.sku)
        self.assertEqual(alert.root_cause["policy_references"][0]["content"], POLICY)
        self.assertEqual(alert.evidence["investigation"], alert.root_cause["evidence"])
        self.assertEqual(self.events(), ["ALERT_DETECTED", "ANALYSIS_STARTED", "ANALYSIS_COMPLETED"])
        self.assertEqual(self.rag.call_args.args[1], INVENTORY_POLICY_QUERY)
        kwargs = self.openai.responses.parse.call_args.kwargs
        self.assertIn("UNTRUSTED DATA", kwargs["instructions"])
        self.assertIn(DATA_FACT, kwargs["input"])
        self.assertNotIn("test-key", kwargs["input"])

    def test_insufficient_evidence_without_supported_mechanism(self):
        self.history(increase=False, late_order=False)
        self.detect(coverage=6.0, demand=10)
        self.openai.responses.parse.return_value.output_parsed = explanation(facts=[AnalysisFact(statement=POLICY, source="POLICY")])
        self.assertEqual(self.post("analizar").status_code, 200)
        analysis = self.db.get(Alert, self.alert.id).root_cause["analysis"]
        self.assertTrue(analysis["insufficient_evidence"])
        self.assertEqual(analysis["root_cause"], "La causa no está demostrada con la evidencia disponible.")
        facts = inventory_facts_catalog(investigate_inventory(self.db, self.alert.evidence, self.cutoff))
        self.assertIn("No hay órdenes de compra retrasadas al corte.", facts)
        self.assertNotIn(DATA_FACT, facts)

    def test_model_cannot_invent_numbers_or_facts(self):
        self.history()
        self.detect()
        evidence = investigate_inventory(self.db, self.alert.evidence, self.cutoff)
        for bad in (explanation(summary="La cobertura es de 3 días."),
                    explanation(facts=[AnalysisFact(statement="El proveedor fue contactado.", source="DATA")]),
                    explanation(policy_findings=[PolicyFinding(statement="Texto inventado", document_name="politica_fixture.pdf", page_number=1, chunk_index=0)])):
            self.openai.responses.parse.return_value.output_parsed = bad
            with self.assertRaises(ValueError):
                explain_inventory(evidence, self.openai, self.policies)
        self.openai.responses.parse.return_value.output_parsed = explanation(summary="La cobertura es de 3 días.")
        with self.assertLogs("app.analyst.service", level="ERROR"):
            self.assertEqual(self.post("analizar").status_code, 503)
        alert = self.db.get(Alert, self.alert.id)
        self.assertEqual((alert.status, alert.root_cause), (AlertStatus.FAILED, None))
        self.assertIn("investigation", alert.evidence)
        self.assertEqual(self.events()[-1], "ANALYSIS_FAILED")

    def test_invalid_detection_evidence_and_roles(self):
        self.history()
        self.detect()
        for role in (Role.AUDITOR, Role.LIDER_PROCESO):
            self.assertEqual(self.post("analizar", role).status_code, 403)
        self.alert.evidence = {**self.alert.evidence, "sku": ""}
        self.db.commit()
        self.assertEqual(self.post("analizar").status_code, 422)
        self.openai.responses.parse.assert_not_called()
        self.assertEqual(self.db.get(Alert, self.alert.id).status, AlertStatus.NEW)

    # --- estratega ------------------------------------------------------------------------
    def test_strategy_requires_analyzing(self):
        self.history()
        self.detect()
        self.assertEqual(self.post("estrategia").status_code, 409)
        self.strategy_client.responses.parse.assert_not_called()

    def test_strategy_persists_inventory_proposals_without_money(self):
        body = self.proposed().json()
        alert = self.db.get(Alert, self.alert.id)
        self.assertEqual((body["status"], alert.status), ("PROPOSED", AlertStatus.PROPOSED))
        self.assertIsNone(alert.amount_at_risk)
        proposals = alert.proposals["proposals"]
        self.assertEqual(len(proposals), 3)
        self.assertTrue(all(p["financial_reference_cop"] is None for p in proposals))
        self.assertTrue({p["action_type"] for p in proposals} <= {a.value for a in InventoryActionType})
        kwargs = self.strategy_client.responses.parse.call_args.kwargs
        self.assertEqual(kwargs["text_format"], InventoryModelStrategy)
        self.assertIn("UNTRUSTED DATA", kwargs["instructions"])
        self.assertEqual(self.events()[-2:], ["STRATEGY_STARTED", "STRATEGY_COMPLETED"])
        self.assertEqual(self.post("estrategia").status_code, 409)
        self.strategy_client.responses.parse.assert_called_once()

    def test_strategy_allowlist_and_size_are_enforced(self):
        raw = inventory_strategy(1).model_dump(mode="json")
        for change in ({"action_type": ActionType.CREATE_PRICE_REVIEW_DRAFT.value}, {"action_type": "SEND_EMAIL"},
                       {"financial_reference_cop": 1000}):
            candidate = {**raw, "proposals": [{**raw["proposals"][0], **change}]}
            self.strategy_client.responses.parse.return_value.output_parsed = candidate
            with self.assertRaises(ValidationError):
                propose_strategy({}, None, self.strategy_client, "INVENTORY_RISK")
        for count in (0, 4):
            with self.assertRaises(ValidationError):
                InventoryModelStrategy.model_validate({**raw, "proposals": [raw["proposals"][0]] * count})
        raw["summary"] = "Se garantiza la reposición en 2 días"
        self.strategy_client.responses.parse.return_value.output_parsed = raw
        with self.assertRaises(ValueError):
            propose_strategy({}, None, self.strategy_client, "INVENTORY_RISK")

    def test_strategy_failure_keeps_analysis_recoverable(self):
        self.analyzed()
        before = self.db.get(Alert, self.alert.id).root_cause
        self.strategy_client.responses.parse.side_effect = RuntimeError("secret")
        with self.assertLogs("app.strategist.service", level="ERROR") as output:
            response = self.post("estrategia")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text + str(output.output))
        alert = self.db.get(Alert, self.alert.id)
        self.assertEqual((alert.status, alert.root_cause, alert.proposals), (AlertStatus.ANALYZING, before, None))
        self.assertEqual(self.events()[-1], "STRATEGY_FAILED")
        self.strategy_client.responses.parse.side_effect = None
        self.assertEqual(self.post("estrategia").status_code, 200)

    # --- ejecutor -------------------------------------------------------------------------
    def test_proposed_and_rejected_never_execute(self):
        self.proposed()
        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(self.post("decision", body={"decision": "REJECTED"}).status_code, 200)
        self.assertEqual(self.db.get(Alert, self.alert.id).status, AlertStatus.REJECTED)
        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(self.actions(), [])

    def test_execution_permissions_by_role_and_area(self):
        self.approved()
        for role in (Role.ANALISTA, Role.AUDITOR):
            self.assertEqual(self.post("ejecutar", role).status_code, 403)
        self.users[Role.LIDER_PROCESO].area = Area.COMERCIAL
        self.db.commit()
        self.assertEqual(self.post("ejecutar", Role.LIDER_PROCESO).status_code, 403)
        self.users[Role.LIDER_PROCESO].area = Area.INVENTARIO
        self.db.commit()
        self.assertEqual(self.post("ejecutar", Role.LIDER_PROCESO).status_code, 200)

    def test_execution_creates_sandbox_actions_from_evidence_and_is_idempotent(self):
        self.approved()
        self.openai.reset_mock()
        self.strategy_client.reset_mock()
        response = self.post("ejecutar")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.get(Alert, self.alert.id).status, AlertStatus.EXECUTED)
        actions = {a.action_type: a.result for a in self.actions()}
        self.assertEqual(len(actions), 3)
        draft = actions[ActionType.CREATE_SUPPLIER_FOLLOWUP_DRAFT.value]
        self.assertEqual((draft["status"], draft["sent"], draft["sandbox"]), ("DRAFT_CREATED", False, True))
        self.assertEqual((draft["sku"], draft["bodega_id"]), (self.sku, self.warehouse))
        self.assertEqual(draft["supplier"], {"proveedor_id": self.supplier, "nombre": "Proveedor de la orden", "origen": "orden_retrasada"})
        self.assertEqual([o[:4] for o in draft["delayed_orders"]], ["OC-L"])
        task = actions[ActionType.CREATE_PURCHASE_ORDER_REVIEW_TASK.value]
        self.assertEqual((task["status"], task["task_type"]), ("TASK_CREATED", "PURCHASE_ORDER_REVIEW"))
        self.openai.responses.parse.assert_not_called()
        self.strategy_client.responses.parse.assert_not_called()
        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(len(self.actions()), 3)
        self.assertEqual(self.events(), ["ALERT_DETECTED", "ANALYSIS_STARTED", "ANALYSIS_COMPLETED", "STRATEGY_STARTED",
                                         "STRATEGY_COMPLETED", "ALERT_DECISION_APPROVED", "EXECUTION_STARTED", "EXECUTION_COMPLETED"])

    def test_execution_failure_keeps_approved(self):
        self.approved()
        with patch("app.executor.service.sandbox_result", side_effect=RuntimeError("secret")), self.assertLogs("app.executor.service", level="ERROR"):
            self.assertEqual(self.post("ejecutar").status_code, 503)
        self.assertEqual((self.db.get(Alert, self.alert.id).status, self.actions()), (AlertStatus.APPROVED, []))
        self.assertEqual(self.events()[-1], "EXECUTION_FAILED")
        self.assertEqual(self.post("ejecutar").status_code, 200)

    def test_edited_proposal_cannot_smuggle_other_scenario_action(self):
        self.proposed()
        edited = {**self.db.get(Alert, self.alert.id).proposals}
        edited["proposals"] = [{**edited["proposals"][0], "action_type": ActionType.CREATE_PRICE_REVIEW_DRAFT.value}]
        self.assertEqual(self.post("decision", body={"decision": "EDITED", "edited_proposal": edited}).status_code, 200)
        self.assertEqual(self.post("decision", body={"decision": "APPROVED"}).status_code, 200)
        self.assertEqual(self.post("ejecutar").status_code, 422)
        self.assertEqual((self.db.get(Alert, self.alert.id).status, self.actions()), (AlertStatus.APPROVED, []))


if __name__ == "__main__":
    unittest.main()
