"""Integración real S4: persiste análisis/propuestas; nunca aprueba ni ejecuta."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.analyst.service import analyze_discount_alert
from app.db.session import SessionLocal
from app.models.core import Alert, AlertStatus, Role, User
from app.rag.service import create_client
from app.strategist.service import generate_strategy
from scripts.integration_margin_openai import print_debug
from scripts.integration_s1_full import CountedClient


def report(alert, stage, calls):
    evidence = alert.evidence if isinstance(alert.evidence, dict) else {}
    investigation = evidence.get("investigation") or {}
    summary = investigation.get("summary") or {}
    print(json.dumps({
        "etapa": stage,
        "estado": alert.status.value,
        "evidencia_resumida": {
            "vendedor_id": evidence.get("vendedor_id"),
            "semana": evidence.get("semana"),
            "lineas_fuera_politica": evidence.get("cantidad_lineas_fuera_politica"),
        },
        "reincidencia": summary.get("reincidencia", evidence.get("trigger_reincidencia")),
        "semanas_consecutivas": summary.get("semanas_consecutivas", evidence.get("semanas_consecutivas")),
        "amount_at_risk": str(alert.amount_at_risk) if alert.amount_at_risk is not None else None,
        "propuestas": alert.proposals,
        "llamadas_openai": calls,
    }, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert-id", type=UUID, required=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    stage = "verificar alerta"
    counted = None
    try:
        with SessionLocal() as db:
            alert = db.get(Alert, args.alert_id)
            if alert is None or alert.type != "DISCOUNT_POLICY_VIOLATION":
                raise ValueError("No existe una alerta real S4 con ese UUID")
            if alert.status not in (AlertStatus.NEW, AlertStatus.ANALYZING, AlertStatus.PROPOSED):
                raise ValueError("La alerta no admite el flujo previo a la decisión humana")
            report(alert, stage, {"responses": 0, "embeddings": 0})
            if alert.status == AlertStatus.PROPOSED:
                print("Ya existen propuestas; no se realizaron llamadas OpenAI.")
                return 0
            user_id = db.scalar(select(User.id).where(
                User.role == Role.GERENTE, User.active.is_(True),
            ).order_by(User.id).limit(1))
            if user_id is None:
                raise ValueError("Falta un gerente activo para atribuir eventos")
            db.commit()
            with create_client() as client:
                counted = CountedClient(client)
                if alert.status == AlertStatus.NEW:
                    stage = "Analista S4: investigación SQL, RAG y explicación"
                    alert = analyze_discount_alert(db, args.alert_id, user_id, client=counted)
                    report(alert, "Analista S4: análisis persistido", counted.calls)
                counted.component = "Estratega S4"
                stage = "Estratega S4: propuestas"
                alert = generate_strategy(db, args.alert_id, user_id, client=counted)
                report(alert, "Estratega S4: propuestas persistidas", counted.calls)
            print("Integración detenida antes de la decisión humana; no se aprobó ni ejecutó nada.")
        return 0
    except Exception as error:
        print("ERROR: integración S4 incompleta; no se aprobó ni ejecutó ninguna acción.", file=sys.stderr)
        if counted:
            print("Llamadas OpenAI: " + json.dumps(counted.calls), file=sys.stderr)
        if args.debug:
            print_debug(counted.error if counted and counted.error is not None else error, stage)
        return 1


if __name__ == "__main__":
    sys.exit(main())
