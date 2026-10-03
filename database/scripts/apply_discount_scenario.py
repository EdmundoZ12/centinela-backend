"""Aplica únicamente 09_discount_scenario.sql sin reconstruir el dataset."""

import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        if not url:
            raise ValueError("Falta DATABASE_URL_UNPOOLED")
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        source = (ROOT / "database/sql/09_discount_scenario.sql").read_text(encoding="utf-8")
        with psycopg.connect(url, connect_timeout=15, autocommit=True) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext('centinela.discount_scenario'))")
                    cursor.execute(source)
        print("09_discount_scenario.sql aplicado; dataset oficial conservado.")
        return 0
    except psycopg.Error as error:
        print(
            f"ERROR al aplicar S4 ({error.sqlstate or 'sin SQLSTATE'}); cambios revertidos.",
            file=sys.stderr,
        )
    except Exception:
        print("ERROR al aplicar S4; verifica conexión, dependencias y permisos.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
