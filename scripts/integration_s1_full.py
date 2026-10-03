"""Integración explícita real: persiste análisis/propuestas; nunca aprueba ni ejecuta."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.analyst.service import analyze_margin_alert
from app.db.session import SessionLocal
from app.models.core import Alert, AlertStatus, AuditLog, Role, User
from app.rag.service import create_client
from app.strategist.service import generate_strategy
from scripts.integration_margin_openai import print_debug


class CountedClient:
    """Contar solicitudes intentadas, incluidas las fallidas; no hay retries SDK."""
    def __init__(self, client):
        self.client = client
        self.calls = {"responses":0, "embeddings":0}
        self.stage = "investigación SQL"
        self.component = "Analista"
        self.error = None
        self.responses = self
        self.embeddings = self

    def parse(self, **kwargs):
        self.calls["responses"] += 1
        self.stage = f"{self.component}: llamada OpenAI"
        try:
            result = self.client.responses.parse(**kwargs)
        except Exception as error:
            self.error = error
            raise
        self.stage = f"{self.component}: Structured Output / validación"
        return result

    def create(self, **kwargs):
        self.calls["embeddings"] += 1
        self.stage = "Analista: recuperación RAG / embeddings"
        try:
            return self.client.embeddings.create(**kwargs)
        except Exception as error:
            self.error = error
            raise


def report(alert, stage, calls):
    print(json.dumps({"etapa":stage, "estado":alert.status.value,
        "root_cause_presente":alert.root_cause is not None,
        "amount_at_risk":str(alert.amount_at_risk) if alert.amount_at_risk is not None else None,
        "propuestas":alert.proposals, "llamadas_openai":calls,
        "total_llamadas_openai":sum(calls.values())}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert-id", type=UUID, required=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    stage = "verificar alerta"
    counted = None
    began_at = datetime.now(timezone.utc)
    try:
        with SessionLocal() as db:
            alert = db.get(Alert,args.alert_id)
            if alert is None or alert.type != "MARGIN_ANOMALY":
                raise ValueError("No existe una alerta real de margen con ese UUID")
            if alert.status not in (AlertStatus.NEW, AlertStatus.ANALYZING, AlertStatus.PROPOSED):
                raise ValueError("La alerta no admite este flujo previo a la decisión humana")
            report(alert,stage,{"responses":0,"embeddings":0})
            if alert.status == AlertStatus.PROPOSED:
                print("Ya existen propuestas; no se realizaron llamadas OpenAI.")
                return 0
            user_id = db.scalar(select(User.id).where(User.role==Role.GERENTE, User.active.is_(True)).order_by(User.id).limit(1))
            if user_id is None:
                raise ValueError("Falta un gerente activo para atribuir los eventos de integración")
            db.commit()
            with create_client() as client:
                counted = CountedClient(client)
                if alert.status == AlertStatus.NEW:
                    stage = "Analista: investigación SQL"
                    alert = analyze_margin_alert(db,args.alert_id,user_id,client=counted)
                    stage = "Analista: análisis persistido"
                    report(alert,stage,counted.calls)
                stage = "Estratega: cálculo de importe persistido"
                counted.component = "Estratega"
                alert = generate_strategy(db,args.alert_id,user_id,client=counted)
                stage = "Estratega: propuestas persistidas"
                report(alert,stage,counted.calls)
            print("Integración terminada antes de la decisión humana. No se aprobó ni ejecutó ninguna acción.")
        return 0
    except Exception as error:
        print("ERROR: integración S1 incompleta; no se aprobó ni ejecutó ninguna acción.", file=sys.stderr)
        if counted:
            print("Llamadas OpenAI: " + json.dumps(counted.calls), file=sys.stderr)
        # Mostrar únicamente el estado persistido, no credenciales ni errores SQL.
        try:
            with SessionLocal() as db:
                alert = db.get(Alert,args.alert_id)
                if alert:
                    event = db.scalar(select(AuditLog).where(AuditLog.alert_id==args.alert_id,
                        AuditLog.created_at >= began_at,
                        AuditLog.event_type.in_(("ANALYSIS_FAILED","STRATEGY_FAILED"))).order_by(AuditLog.created_at.desc()).limit(1))
                    if counted:
                        stage = counted.stage if sum(counted.calls.values()) else stage
                    if event and event.payload.get("stage") in ("investigation","policy_retrieval","persistence"):
                        stage = {"investigation":"Analista: investigación SQL", "policy_retrieval":"Analista: recuperación RAG", "persistence":"Analista: persistencia"}[event.payload["stage"]]
                    report(alert,stage,counted.calls if counted else {"responses":0,"embeddings":0})
        except Exception:
            print("No se pudo consultar el estado final.",file=sys.stderr)
        if args.debug:
            print_debug(counted.error if counted and counted.error is not None else error, stage)
        return 1


if __name__ == "__main__":
    sys.exit(main())
