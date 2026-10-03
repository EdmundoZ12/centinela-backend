"""Prueba opcional de investigación real, read-only y sin llamadas a OpenAI."""

import os
import unittest
from datetime import timedelta
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.analyst.margin import investigate_margin, percent_change


@unittest.skipUnless(os.environ.get("CENTINELA_TEST_POSTGRES") == "1", "Requiere CENTINELA_TEST_POSTGRES=1")
class AnalystPostgresTests(unittest.TestCase):
    def test_readonly_official_evidence(self):
        root = Path(__file__).resolve().parents[1]
        url = make_url(dotenv_values(root / ".env")["DATABASE_URL_UNPOOLED"]).set(drivername="postgresql+psycopg")
        engine = create_engine(url, connect_args={"connect_timeout": 15})
        try:
            with Session(engine) as db:
                db.execute(text("SET TRANSACTION READ ONLY"))
                cutoff = db.scalar(text("SELECT centinela.fecha_corte()"))
                week = cutoff - timedelta(days=cutoff.weekday())
                line = db.scalar(text("SELECT linea FROM centinela.v_ventas WHERE fecha>=:week AND fecha<=:cutoff ORDER BY linea LIMIT 1"), {"week": week, "cutoff": cutoff})
                self.assertIsNotNone(line)
                evidence = investigate_margin(db, line, week, cutoff)
                actual = db.scalar(text(
                    "SELECT round(100*(1-sum(costo_total)/nullif(sum(valor_neto),0)),2) "
                    "FROM centinela.v_ventas WHERE linea=:line AND fecha>=:week AND fecha<=:cutoff"
                ), {"line": line, "week": week, "cutoff": cutoff})
                self.assertEqual(evidence.margen_actual_pct, actual)
                self.assertTrue(evidence.sku_afectados)
                self.assertTrue(all(start < week for start in evidence.semanas_historicas))
                for sku in evidence.sku_afectados:
                    self.assertEqual(sku.variacion_costo_pct, percent_change(sku.costo_anterior, sku.costo_actual))
                    self.assertTrue(all(provider.fecha_vigencia_actual <= cutoff for provider in sku.proveedores))
                db.rollback()
        except Exception:
            raise AssertionError("No se pudo validar la investigación sobre PostgreSQL; revisar conexión y dataset.") from None
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
