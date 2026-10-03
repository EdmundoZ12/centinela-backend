"""Inicializa el dataset oficial; nunca reemplaza relaciones existentes."""

import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "database"
EXPECTED_VIEWS = {
    "v_ventas", "v_margen_semanal_linea", "v_cartera_cliente",
    "v_dias_pago_mensual", "v_cobertura_inventario",
    "v_descuentos_fuera_politica", "v_actividad_cliente",
}
NAME = r"[a-z_][a-z_0-9]*"
SET_PATH = re.compile(rf"SET\s+search_path\s+TO\s+({NAME})\s*;", re.I)
COPY = re.compile(
    rf"\\copy\s+({NAME}(?:\.{NAME})?)\s*\(([^)]+)\)\s+"
    r"FROM\s+'([^']+)'\s+WITH\s*"
    r"\(\s*FORMAT\s+csv\s*,\s*HEADER\s+true\s*,\s*ENCODING\s+'UTF8'\s*\)\s*;",
    re.I,
)


class SetupError(Exception):
    """Mensaje controlado que no contiene datos de conexión."""


@dataclass(frozen=True)
class Load:
    schema: str
    table: str
    columns: tuple[str, ...]
    path: Path


def parse_loads(source: str) -> tuple[str, list[Load]]:
    """Acepta la sintaxis oficial; rechaza comandos nuevos en lugar de omitirlos."""
    schema = None
    loads = []
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        if match := SET_PATH.fullmatch(line):
            schema = match[1]
            continue
        match = COPY.fullmatch(line)
        if not match or schema is None:
            raise SetupError(f"Comando no soportado en 02_carga.sql, línea {number}.")
        parts = match[1].split(".")
        target_schema, table = (schema, parts[0]) if len(parts) == 1 else parts
        if target_schema != schema:
            raise SetupError("La carga utiliza más de un esquema; revisar el SQL oficial.")
        columns = tuple(column.strip() for column in match[2].split(","))
        if not all(re.fullmatch(NAME, column) for column in columns):
            raise SetupError(f"Columnas no soportadas en la carga de {table}.")
        path = (DATABASE / match[3]).resolve()
        if path.parent != (DATABASE / "csv").resolve():
            raise SetupError(f"Ruta CSV fuera de database/csv para {table}.")
        loads.append(Load(schema, table, columns, path))
    if not schema or not loads:
        raise SetupError("02_carga.sql no define un esquema y cargas válidas.")
    if len({load.table for load in loads}) != len(loads):
        raise SetupError("02_carga.sql contiene cargas repetidas para una tabla.")
    return schema, loads


def validate_csvs(loads: list[Load]) -> None:
    for load in loads:
        if not load.path.is_file():
            raise SetupError(f"Falta el CSV oficial: {load.path.name}.")
        with load.path.open(encoding="utf-8", newline="") as source:
            header = next(csv.reader(source), None)
        if header != list(load.columns):
            raise SetupError(f"El encabezado de {load.path.name} no coincide con 02_carga.sql.")


def initialize(connection, schema: str, loads: list[Load], ddl: str, semantic: str) -> list[str]:
    report = []
    with connection.transaction():
        with connection.cursor() as cursor:
            # Evita que dos ejecuciones de este script inicialicen simultáneamente.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"centinela.setup.{schema}",))
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)", (schema,))
            schema_exists = cursor.fetchone()[0]
            cursor.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') ORDER BY c.relname",
                (schema,),
            )
            existing = [row[0] for row in cursor.fetchall()]
            if existing:
                raise SetupError(
                    f"El esquema {schema} ya contiene relaciones: {', '.join(existing)}. "
                    "Inicialización cancelada; no se reemplazaron ni eliminaron datos."
                )
            if schema_exists:
                print(f"El esquema {schema} ya existe y está vacío; se utilizará.")
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(ddl)
            for load in loads:
                command = sql.SQL(
                    "COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"
                ).format(
                    sql.Identifier(load.schema, load.table),
                    sql.SQL(", ").join(map(sql.Identifier, load.columns)),
                )
                with load.path.open("rb") as source, cursor.copy(command) as copy:
                    while chunk := source.read(1024 * 1024):
                        copy.write(chunk)
            cursor.execute(semantic)
            for load in loads:
                cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, load.table)))
                count = cursor.fetchone()[0]
                if count == 0:
                    raise SetupError(f"La tabla {load.table} quedó vacía; inicialización cancelada.")
                report.append(f"{schema}.{load.table}: {count} filas")
            cursor.execute("SELECT viewname FROM pg_views WHERE schemaname = %s ORDER BY viewname", (schema,))
            views = {row[0] for row in cursor.fetchall()}
            missing = EXPECTED_VIEWS - views
            if missing:
                raise SetupError(f"Faltan vistas semánticas: {', '.join(sorted(missing))}.")
            report.append("Vistas semánticas: " + ", ".join(sorted(views)))
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vector = cursor.fetchone()
            if vector is None:
                raise SetupError("No se encontró la extensión vector.")
            report.append(f"Extensión vector: instalada (versión {vector[0]})")
            # Esta tabla define el horizonte del dataset en fecha_corte() del SQL oficial.
            cursor.execute(sql.SQL("SELECT min(fecha), max(fecha) FROM {}").format(
                sql.Identifier(schema, "inventario_diario")
            ))
            minimum, maximum = cursor.fetchone()
            report.append(f"Dataset (inventario_diario.fecha): fecha mínima {minimum}; fecha máxima {maximum}")
    return report


def main() -> int:
    stage = "lectura de configuración y archivos oficiales"
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        if not url:
            raise SetupError("Falta DATABASE_URL_UNPOOLED en el .env del proyecto.")
        # psycopg recibe una URI PostgreSQL, sin el sufijo específico de SQLAlchemy.
        if url.startswith("postgresql+psycopg://"):
            url = "postgresql://" + url.removeprefix("postgresql+psycopg://")
        ddl = (DATABASE / "sql" / "01_esquema.sql").read_text(encoding="utf-8")
        loading = (DATABASE / "sql" / "02_carga.sql").read_text(encoding="utf-8")
        semantic = (DATABASE / "sql" / "03_capa_semantica.sql").read_text(encoding="utf-8")
        schema, loads = parse_loads(loading)
        validate_csvs(loads)
        stage = "conexión administrativa"
        with psycopg.connect(url, connect_timeout=15, autocommit=True) as connection:
            stage = "inicialización transaccional"
            report = initialize(connection, schema, loads, ddl, semantic)
        print("Inicialización completada y confirmada.")
        for line in report:
            print(line)
        return 0
    except SetupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
    except psycopg.Error as error:
        code = error.sqlstate or "sin SQLSTATE"
        print(
            f"ERROR durante {stage} ({code}). No se confirmó ningún cambio de esta ejecución. "
            "Revisa conectividad, permisos, extensión vector y archivos oficiales.",
            file=sys.stderr,
        )
    except Exception:
        # No imprimir excepciones arbitrarias: pueden contener una URI o datos sensibles.
        print(f"ERROR durante {stage}. No se confirmó ningún cambio de esta ejecución.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
