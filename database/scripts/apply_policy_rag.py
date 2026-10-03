"""Aplica únicamente 07_policy_rag.sql, sin reconstruir el dataset."""
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def main():
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        if not url:
            raise ValueError("Falta DATABASE_URL_UNPOOLED")
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=15) as db:
            db.execute((ROOT / "database/sql/07_policy_rag.sql").read_text(encoding="utf-8"))
        print("Esquema de políticas aplicado; dataset conservado.")
        return 0
    except Exception:
        print("ERROR: no se pudo aplicar el esquema de políticas. Verifica DATABASE_URL_UNPOOLED y permisos; cambios revertidos.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
