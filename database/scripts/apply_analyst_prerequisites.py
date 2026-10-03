"""Aplica solo las tablas faltantes del core; no crea alertas ni carga CSV."""

import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        if not url:
            print("ERROR: falta DATABASE_URL_UNPOOLED.", file=sys.stderr)
            return 1
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        source = (ROOT / "database/sql/07_analyst_prerequisites.sql").read_text(encoding="utf-8")
        with psycopg.connect(url, connect_timeout=15) as connection:
            connection.execute(source)
        print("Prerrequisitos del Analista aplicados; datos existentes conservados.")
        return 0
    except Exception:
        print("ERROR: no se pudieron aplicar los prerrequisitos; no se confirmó ningún cambio.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
