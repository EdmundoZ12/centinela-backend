"""Aplica únicamente 04_reloj_simulado.sql, sin recargar el dataset."""

import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        if not url:
            print("ERROR: falta DATABASE_URL_UNPOOLED en el .env del proyecto.", file=sys.stderr)
            return 1
        if url.startswith("postgresql+psycopg://"):
            url = "postgresql://" + url.removeprefix("postgresql+psycopg://")
        source = (ROOT / "database/sql/04_reloj_simulado.sql").read_text(encoding="utf-8")
        with psycopg.connect(url, connect_timeout=15, autocommit=True) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext('centinela.simulation_clock'))")
                    cursor.execute(source)
                    cursor.execute('SELECT "current_date" FROM app.simulation_state WHERE id = 1')
                    current = cursor.fetchone()[0]
        print(f"Reloj aplicado. Fecha actual: {current}; fecha máxima: 2026-09-30.")
        return 0
    except psycopg.Error as error:
        print(
            f"ERROR al aplicar el reloj ({error.sqlstate or 'sin SQLSTATE'}). "
            "No se confirmó ningún cambio. Verifica conexión, permisos y dataset inicializado.",
            file=sys.stderr,
        )
    except Exception:
        print("ERROR al aplicar el reloj. No se confirmó ningún cambio.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
