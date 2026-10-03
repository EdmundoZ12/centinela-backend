"""Integración explícita: evidencia real + OpenAI; no escribe en PostgreSQL.

Uso: python scripts/integration_margin_openai.py --alert-id UUID
Consume una llamada real a OpenAI y usa la configuración del .env del proyecto.
"""

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Date, select, text

from app.analyst.margin import investigate_margin
from app.analyst.service import explain_margin
from app.db.session import SessionLocal
from app.models.core import Alert
from app.rag.service import MARGIN_POLICY_QUERY, retrieve_policies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert-id", type=UUID, required=True)
    args = parser.parse_args()
    try:
        with SessionLocal() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            alert = db.scalar(select(Alert).where(Alert.id == args.alert_id))
            if alert is None or alert.type != "MARGIN_ANOMALY":
                print("ERROR: no existe una alerta de margen con ese UUID.", file=sys.stderr)
                return 1
            from datetime import date
            line = alert.evidence["linea"]
            week = date.fromisoformat(alert.evidence["semana"])
            if alert.simulated_date > db.scalar(text("SELECT centinela.fecha_corte() AS cutoff").columns(cutoff=Date())):
                raise ValueError("Corte futuro")
            evidence = investigate_margin(db, line, week, alert.simulated_date)
            policies = retrieve_policies(db, MARGIN_POLICY_QUERY)
            db.rollback()
        result = explain_margin(evidence, policies=policies)
        import json
        print(json.dumps({"analysis": result.model_dump(mode="json"),
                          "evidence": evidence.model_dump(mode="json"),
                          "policy_references": [p.model_dump() for p in policies]}, ensure_ascii=False, indent=2))
        print("Integración completada; no se modificó la alerta ni se escribieron datos.")
        return 0
    except Exception:
        print("ERROR: falló la integración. Verifica configuración, permisos del modelo y datos; no se escribió en PostgreSQL.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
