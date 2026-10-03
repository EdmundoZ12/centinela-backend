import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text
from sqlalchemy import Date, bindparam
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import test_decisions as sqlite_support
from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AuditLog, Base, Role, User
from app.vigil.detectors.margin import MARGIN_QUERY, MarginDetector, margin_evidence
from app.vigil.service import run_vigil


class MarginTests(unittest.IsolatedAsyncioTestCase):
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
        def sqlite_syntax(connection, cursor, statement, parameters, context, executemany):
            return statement.replace("centinela.fecha_corte()", "fecha_corte()").replace(" FOR SHARE", ""), parameters

        Base.metadata.create_all(cls.engine)
        with cls.engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE app.simulation_state (id INTEGER PRIMARY KEY, "current_date" DATE)')
            connection.exec_driver_sql("INSERT INTO app.simulation_state VALUES (1, '2026-08-20')")
            connection.exec_driver_sql("CREATE TABLE centinela.test_sales (fecha DATE, semana DATE, linea TEXT, ventas NUMERIC, costo NUMERIC)")
            connection.exec_driver_sql("CREATE TABLE centinela.ref_margen_minimo_linea (linea TEXT PRIMARY KEY, margen_minimo_pct NUMERIC)")
            connection.exec_driver_sql("""
                CREATE VIEW centinela.v_margen_semanal_linea AS
                SELECT semana, linea, round(100 * (1 - 1.0 * sum(costo) / sum(ventas)), 2) AS margen_pct
                FROM test_sales WHERE fecha <= fecha_corte() GROUP BY semana, linea
            """)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        type(self).cutoff = date(2026, 8, 20)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.connection.exec_driver_sql("BEGIN")
        self.db = Session(bind=self.connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
        self.line = "Línea de prueba " + str(uuid4())
        self.week = self.cutoff - timedelta(days=self.cutoff.weekday())
        self.users = {}
        for role in Role:
            user = User(name=role.value, email=f"{uuid4()}@test.invalid", role=role, active=True)
            self.db.add(user)
            self.users[role] = user
        self.db.commit()
        app.dependency_overrides[get_db] = lambda: self.db

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def sale(self, week, margin, day=None):
        self.db.execute(text("INSERT INTO centinela.test_sales VALUES (:fecha, :semana, :linea, :ventas, :costo)"), {
            "fecha": (day or week).isoformat(), "semana": week.isoformat(), "linea": self.line,
            "ventas": 100.0, "costo": float(Decimal("100") - Decimal(str(margin))),
        })

    def fixture(self, current=20, history=None, minimum=15):
        self.sale(self.week, current)
        for index, margin in enumerate(history if history is not None else [25] * 8, 1):
            self.sale(self.week - timedelta(weeks=index), margin)
        if minimum is not None:
            self.db.execute(text("INSERT INTO centinela.ref_margen_minimo_linea VALUES (:linea, :minimum)"), {"linea": self.line, "minimum": minimum})
        self.db.commit()

    def margin_rows(self):
        return self.db.execute(MARGIN_QUERY.bindparams(bindparam("week", type_=Date())), {"week": self.week}).mappings().all()

    def detected_alerts(self):
        return self.db.scalars(select(Alert).where(Alert.type == "MARGIN_ANOMALY")).all()

    async def request(self, role):
        # Reutiliza únicamente el transporte ASGI; no hereda otros tests.
        self.alert_id = uuid4()
        self.users["selected"] = self.users[role]
        return await sqlite_support.DecisionTests.request(self, path="/vigia/ejecutar", user="selected")

    async def test_average_uses_exactly_last_eight_available_weeks(self):
        self.fixture(history=list(range(21, 29)))
        self.sale(self.week - timedelta(weeks=9), 99)
        self.sale(self.week + timedelta(weeks=1), 99)
        row = self.margin_rows()[0]
        self.assertEqual(row["margen_promedio_8_semanas_pct"], Decimal("24.5"))
        self.assertEqual(margin_evidence(row)["caida_pp"], 4.5)

    async def test_strict_three_point_boundary(self):
        self.fixture(current=22, history=[25] * 8, minimum=15)
        self.assertIsNone(margin_evidence(self.margin_rows()[0]))
        self.db.execute(text("UPDATE centinela.test_sales SET costo=78.01 WHERE semana=:week"), {"week": self.week.isoformat()})
        self.assertTrue(margin_evidence(self.margin_rows()[0])["trigger_caida"])

    async def test_minimum_boundary_and_no_history(self):
        self.fixture(current=14, history=[], minimum=15)
        evidence = margin_evidence(self.margin_rows()[0])
        self.assertTrue(evidence["trigger_margen_minimo"])
        self.assertFalse(evidence["trigger_caida"])
        self.assertIsNone(evidence["margen_promedio_8_semanas_pct"])
        self.db.execute(text("UPDATE centinela.test_sales SET costo=85"))
        self.assertIsNone(margin_evidence(self.margin_rows()[0]))

    async def test_future_data_in_same_week_is_excluded(self):
        self.fixture(current=20)
        self.sale(self.week, -50, day=self.cutoff + timedelta(days=1))
        self.sale(self.week + timedelta(weeks=1), -50)
        row = self.margin_rows()[0]
        self.assertEqual(row["margen_actual_pct"], Decimal("20"))
        result = run_vigil(self.db)
        self.assertEqual(result.alertas_nuevas, 1)
        self.assertEqual(self.detected_alerts()[0].simulated_date, self.cutoff)

    async def test_persists_real_alert_null_analysis_and_single_audit(self):
        self.fixture()
        self.assertEqual(run_vigil(self.db).alertas_nuevas, 1)
        alert = self.detected_alerts()[0]
        self.assertEqual((alert.area.value, alert.status.value, alert.severity), ("COMERCIAL", "NEW", "HIGH"))
        self.assertTrue(all(value is None for value in [alert.confidence, alert.amount_at_risk, alert.root_cause, alert.proposals]))
        self.assertEqual(alert.evidence["margen_actual_pct"], 20)
        self.assertEqual(run_vigil(self.db).alertas_nuevas, 0)
        self.assertEqual(len(self.detected_alerts()), 1)
        logs = self.db.scalars(select(AuditLog).where(AuditLog.event_type == "ALERT_DETECTED")).all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].alert_id, alert.id)

    async def test_new_week_has_different_dedupe_key(self):
        self.fixture(current=10, history=[], minimum=15)
        self.assertEqual(run_vigil(self.db).alertas_nuevas, 1)
        type(self).cutoff += timedelta(weeks=1)
        self.sale(self.week + timedelta(weeks=1), 10)
        self.db.commit()
        self.assertEqual(run_vigil(self.db).alertas_nuevas, 1)
        self.assertEqual(len({a.dedupe_key for a in self.detected_alerts()}), 2)

    async def test_manual_endpoint_roles(self):
        self.fixture()
        for role in [Role.LIDER_PROCESO, Role.AUDITOR]:
            self.assertEqual((await self.request(role))[0], 403)
        for role in [Role.GERENTE, Role.ANALISTA]:
            code, body = await self.request(role)
            self.assertEqual(code, 200)
            self.assertEqual(body["detectores_ejecutados"], 1)

    async def test_failure_is_controlled_logged_and_next_detector_runs(self):
        class Broken:
            name = "margin_detector"
            def run(self, db, user_id):
                raise RuntimeError("secret-password")

        class Next:
            name = "test_next"
            def run(self, db, user_id):
                return 0

        with self.assertLogs("app.vigil.service", level="ERROR") as logs:
            result = run_vigil(self.db, detectors=[Broken(), Next()])
        self.assertEqual(result.detectores_ejecutados, 2)
        self.assertEqual(result.errores, ["margin_detector"])
        self.assertNotIn("secret-password", str(logs.output))
        row = self.db.scalar(select(AuditLog).where(AuditLog.event_type == "DETECTOR_FAILED"))
        self.assertEqual(row.payload["detector"], "margin_detector")

    async def test_audit_failure_rolls_back_new_alert(self):
        self.fixture()
        real_flush = self.db.flush

        def flush(*args, **kwargs):
            if any(isinstance(row, AuditLog) and row.event_type == "ALERT_DETECTED" for row in self.db.new):
                raise RuntimeError("test failure")
            return real_flush(*args, **kwargs)

        with patch.object(self.db, "flush", side_effect=flush), self.assertLogs("app.vigil.service", level="ERROR"):
            result = run_vigil(self.db)
        self.assertEqual(result.alertas_nuevas, 0)
        self.assertEqual(self.detected_alerts(), [])


if __name__ == "__main__":
    unittest.main()
