"""Integración explícita: evidencia real + OpenAI; no escribe en PostgreSQL.

Uso: python scripts/integration_margin_openai.py --alert-id UUID
Consume una llamada real a OpenAI y usa la configuración del .env del proyecto.
"""

import argparse
import json
import linecache
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Date, select, text
from dotenv import dotenv_values
from pydantic import ValidationError

from app.analyst.margin import investigate_margin
from app.analyst.service import explain_margin
from app.db.session import SessionLocal
from app.models.core import Alert
from app.rag.service import MARGIN_POLICY_QUERY, retrieve_policies


def safe_diagnostic(message: str) -> str:
    """Redactar valores conocidos y patrones sensibles; nunca imprimir configuración."""
    values = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    from app.core.config import settings
    for name in ("database_url", "openai_api_key"):
        secret = getattr(settings, name, None)
        if secret:
            values[name.upper()] = secret.get_secret_value()
    secrets = set()
    for name, value in values.items():
        if value and re.search(r"KEY|SECRET|TOKEN|PASSWORD|DATABASE_URL|AUTHORIZATION", name, re.I):
            secrets.add(value)
            if "DATABASE_URL" in name.upper():
                try:
                    password = urlsplit(value).password
                    if password:
                        secrets.update((password, unquote(password)))
                except ValueError:
                    pass
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "[REDACTADO]")
    message = re.sub(r"postgres(?:ql)?(?:\+[\w]+)?://[^\s\"'<>]+", "[URL REDACTADA]", message, flags=re.I)
    message = re.sub(r"sk-[\w-]+", "[CLAVE REDACTADA]", message)
    message = re.sub(r"(?im)^.*(?:authorization|api[_-]?key|database_url(?:_unpooled)?|password|secret|access[_-]?token)\s*[\"']?\s*[:=].*$", "[CAMPO SENSIBLE REDACTADO]", message)
    message = re.sub(r"(?i)Bearer\s+[^\s\"',;}]+", "[AUTORIZACIÓN REDACTADA]", message)
    # No permitir secuencias de control que oculten texto o alteren la terminal.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", message)


def failure_stage(stage: str, error: Exception) -> str:
    if stage != "llamada OpenAI":
        return stage
    if isinstance(error, (ValidationError, json.JSONDecodeError)) or type(error).__name__ == "APIResponseValidationError":
        return "Structured Output / validación"
    trace = error.__traceback__
    while trace:
        frame = trace.tb_frame
        if frame.f_code.co_name == "explain_margin":
            source = linecache.getlines(frame.f_code.co_filename)
            boundary = next((i for i, line in enumerate(source, 1) if "if result.status" in line), None)
            if boundary and trace.tb_lineno >= boundary:
                return "Structured Output / validación"
        trace = trace.tb_next
    return stage


def print_debug(error: Exception, stage: str) -> None:
    print("Etapa: " + failure_stage(stage, error), file=sys.stderr)
    # Solo ubicaciones del traceback: sin variables locales, código fuente, headers
    # ni representaciones de clientes. Incluir las causas facilita diagnosticar SQL/HTTP.
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        print("Excepción: " + type(current).__name__, file=sys.stderr)
        print("Mensaje seguro: " + safe_diagnostic(str(current)), file=sys.stderr)
        print("Traceback (sin variables locales):", file=sys.stderr)
        trace = current.__traceback__
        while trace:
            code = trace.tb_frame.f_code
            print(safe_diagnostic(f"  {code.co_filename}:{trace.tb_lineno} en {code.co_name}"), file=sys.stderr)
            trace = trace.tb_next
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert-id", type=UUID, required=True)
    parser.add_argument("--debug", action="store_true", help="Mostrar etapa, excepción y traceback sin secretos")
    args = parser.parse_args()
    stage = "cargar alerta"
    try:
        with SessionLocal() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            alert = db.scalar(select(Alert).where(Alert.id == args.alert_id))
            if alert is None or alert.type != "MARGIN_ANOMALY":
                print("ERROR: no existe una alerta de margen con ese UUID.", file=sys.stderr)
                return 1
            from datetime import date
            stage = "investigación SQL"
            line = alert.evidence["linea"]
            week = date.fromisoformat(alert.evidence["semana"])
            if alert.simulated_date > db.scalar(text("SELECT centinela.fecha_corte() AS cutoff").columns(cutoff=Date())):
                raise ValueError("Corte futuro")
            evidence = investigate_margin(db, line, week, alert.simulated_date)
            stage = "recuperación RAG"
            policies = retrieve_policies(db, MARGIN_POLICY_QUERY)
            db.rollback()
        stage = "llamada OpenAI"
        result = explain_margin(evidence, policies=policies)
        stage = "Structured Output / validación"
        print(json.dumps({"analysis": result.model_dump(mode="json"),
                          "evidence": evidence.model_dump(mode="json"),
                          "policy_references": [p.model_dump() for p in policies]}, ensure_ascii=False, indent=2))
        print("Integración completada; no se modificó la alerta ni se escribieron datos.")
        return 0
    except Exception as error:
        print("ERROR: falló la integración. Verifica configuración, permisos del modelo y datos; no se escribió en PostgreSQL.", file=sys.stderr)
        if args.debug:
            print_debug(error, stage)
        return 1


if __name__ == "__main__":
    sys.exit(main())
