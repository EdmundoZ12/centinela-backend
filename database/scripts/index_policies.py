"""Indexación explícita: consume embeddings solo para documentos nuevos o modificados."""
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.rag.policies import index_document
from app.rag.service import create_client


def main():
    try:
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL_UNPOOLED")
        files = sorted((ROOT / "policies").glob("*.pdf"))
        if not url or not files or not settings.openai_embedding_model:
            raise ValueError("Configuración o PDFs ausentes")
        with create_client() as client, psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True, connect_timeout=15
        ) as db:
            for path in files:
                count = index_document(db, path, client)
                print(f"{path.name}: {count} chunks indexados" if count else f"{path.name}: sin cambios")
        return 0
    except Exception:
        print("ERROR: indexación incompleta. Verifica esquema, PDFs y configuración de OpenAI. El documento fallido conserva su versión anterior.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
