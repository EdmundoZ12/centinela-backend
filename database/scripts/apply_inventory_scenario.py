"""Aplica únicamente 09_inventory_scenario.sql sin reconstruir el dataset."""
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def main():
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        if not url:
            raise ValueError("Falta conexión administrativa")
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=15) as db:
            db.execute((ROOT / "database/sql/09_inventory_scenario.sql").read_text(encoding="utf-8"))
        print("09_inventory_scenario.sql aplicado; dataset conservado.")
        return 0
    except Exception:
        print("ERROR: no se pudo aplicar el SQL de inventario; transacción revertida. Verifica conexión y permisos.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
