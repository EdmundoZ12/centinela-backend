"""Validación opcional sobre el dataset oficial; siempre termina en rollback."""

import os
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.core import Alert, AuditLog
from app.api.simulation import advance_simulation
from app.vigil.detectors.margin import MARGIN_QUERY, margin_evidence
from app.vigil.service import run_vigil


@unittest.skipUnless(os.environ.get("CENTINELA_TEST_POSTGRES") == "1", "Requiere CENTINELA_TEST_POSTGRES=1")
class OfficialMarginTests(unittest.TestCase):
    def test_official_metrics_detection_idempotence_and_no_future_data(self):
        root = Path(__file__).resolve().parents[1]
        url = make_url(dotenv_values(root / ".env")["DATABASE_URL_UNPOOLED"]).set(drivername="postgresql+psycopg")
        engine = create_engine(url, connect_args={"connect_timeout": 15})
        try:
            try:
                connection = engine.connect()
            except SQLAlchemyError:
                raise AssertionError("No se pudo conectar a PostgreSQL para validar el detector.") from None
            with connection:
                transaction = connection.begin()
                try:
                    with connection.connection.driver_connection.cursor() as cursor:
                        for name in ["04_reloj_simulado.sql", "05_core_app.sql", "06_margin_detector.sql"]:
                            cursor.execute((root / "database/sql" / name).read_text(encoding="utf-8"))
                        cursor.execute((root / "database/sql/06_margin_detector.sql").read_text(encoding="utf-8"))
                    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
                        cutoff = date(2026, 8, 20)
                        week = cutoff - timedelta(days=cutoff.weekday())
                        db.execute(text('UPDATE app.simulation_state SET "current_date"=:cutoff WHERE id=1'), {"cutoff": cutoff})
                        rows = db.execute(MARGIN_QUERY, {"week": week}).mappings().all()
                        self.assertTrue(rows, "El dataset oficial debe tener métricas en la fecha de prueba.")
                        for row in rows:
                            history = db.execute(text(
                                "SELECT margen_pct FROM centinela.v_margen_semanal_linea "
                                "WHERE linea=:line AND semana<:week AND margen_pct IS NOT NULL "
                                "ORDER BY semana DESC LIMIT 8"
                            ), {"line": row["linea"], "week": week}).scalars().all()
                            average = sum(history) / Decimal(len(history)) if history else None
                            self.assertEqual(row["margen_promedio_8_semanas_pct"], average)
                            # Reproducción independiente del agregado desde las ventas ya acotadas.
                            actual = db.scalar(text(
                                "SELECT round(100*(1-sum(costo_total)/nullif(sum(valor_neto),0)),2) "
                                "FROM centinela.v_ventas WHERE linea=:line AND fecha>=:week AND fecha<=:cutoff"
                            ), {"line": row["linea"], "week": week, "cutoff": cutoff})
                            self.assertEqual(row["margen_actual_pct"], actual)
                        self.assertEqual(db.scalar(text(
                            "SELECT count(*) FROM centinela.v_ventas WHERE fecha>centinela.fecha_corte()"
                        )), 0)
                        candidates = [margin_evidence(row) for row in rows if margin_evidence(row) is not None]
                        self.assertTrue(candidates, "La fecha de prueba debe producir una anomalía oficial.")
                        before_ids = set(db.scalars(select(Alert.id)).all())
                        before_logs = set(db.scalars(select(AuditLog.id)).all())
                        result = run_vigil(db)
                        self.assertEqual(result.errores, [])
                        alerts = db.scalars(select(Alert).where(Alert.id.not_in(before_ids))).all()
                        self.assertEqual(result.alertas_nuevas, len(alerts))
                        logs = db.scalars(select(AuditLog).where(AuditLog.id.not_in(before_logs))).all()
                        self.assertEqual(len(logs), len(alerts))
                        for alert in alerts:
                            self.assertEqual(alert.simulated_date, cutoff)
                            self.assertIn(alert.evidence, candidates)
                            self.assertIsNone(alert.root_cause)
                            self.assertIsNone(alert.proposals)
                        self.assertTrue(all(log.event_type == "ALERT_DETECTED" for log in logs))
                        self.assertEqual(run_vigil(db).alertas_nuevas, 0)
                        self.assertEqual(set(db.scalars(select(AuditLog.id)).all()), before_logs | {log.id for log in logs})
                        advanced = advance_simulation(db=db, dias=1)
                        self.assertEqual(advanced.fecha_actual, cutoff + timedelta(days=1))
                        self.assertEqual(advanced.vigia.detectores_ejecutados, 1)
                        self.assertEqual(advanced.vigia.errores, [])

                        class Broken:
                            name = "test_failure"

                            def run(self, db, user_id):
                                raise RuntimeError("sensitive-marker")

                        with patch("app.vigil.service.DETECTORS", (Broken(),)), self.assertLogs("app.vigil.service", level="ERROR") as output:
                            advanced = advance_simulation(db=db, dias=1)
                        self.assertEqual(advanced.fecha_actual, cutoff + timedelta(days=2))
                        self.assertEqual(advanced.vigia.errores, ["test_failure"])
                        self.assertEqual(db.scalar(text("SELECT centinela.fecha_corte()")), advanced.fecha_actual)
                        self.assertNotIn("sensitive-marker", str(output.output))
                        self.assertIsNotNone(db.scalar(select(AuditLog).where(
                            AuditLog.event_type == "DETECTOR_FAILED",
                            AuditLog.payload["detector"].astext == "test_failure",
                        )))
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
