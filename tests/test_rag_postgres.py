"""Opcional: pgvector e indexación reales con embeddings mockeados y rollback."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.rag.policies import extract_chunks, index_document
from app.rag.service import retrieve_policies


@unittest.skipUnless(os.environ.get("CENTINELA_TEST_POSTGRES") == "1", "Requiere CENTINELA_TEST_POSTGRES=1")
class PolicyPostgresTests(unittest.TestCase):
    def test_real_vector_index_idempotence_retrieval_and_rollback(self):
        root = Path(__file__).resolve().parents[1]
        url = make_url(dotenv_values(root / ".env")["DATABASE_URL_UNPOOLED"]).set(drivername="postgresql+psycopg")
        engine = create_engine(url, connect_args={"connect_timeout": 15})
        client = MagicMock()
        try:
            with engine.connect() as connection, tempfile.TemporaryDirectory() as directory:
                transaction = connection.begin()
                try:
                    connection.exec_driver_sql((root / "database/sql/07_policy_rag.sql").read_text(encoding="utf-8"))
                    path = Path(directory) / f"fixture-{uuid4()}.pdf"
                    path.write_bytes(next((root / "policies").glob("*.pdf")).read_bytes())
                    chunks = extract_chunks(path)
                    client.embeddings.create.return_value.data = [SimpleNamespace(index=i, embedding=[1.0, .2+i*.1]) for i in range(len(chunks))]
                    with patch.object(settings, "openai_embedding_model", f"test-{uuid4()}"):
                        driver = connection.connection.driver_connection
                        self.assertEqual(index_document(driver, path, client), len(chunks))
                        self.assertEqual(index_document(driver, path, client), 0)
                        client.embeddings.create.assert_called_once()
                        client.embeddings.create.return_value.data = [SimpleNamespace(index=0, embedding=[1.0, .2])]
                        with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
                            result = retrieve_policies(db, "política", client)
                            self.assertEqual(result[0].document_name, path.name)
                            self.assertEqual(result[0].chunk_index, 0)
                            self.assertEqual(result[0].page_number, 1)
                            self.assertEqual(result[0].content, chunks[0]["content"])
                finally:
                    transaction.rollback()
        except Exception:
            raise AssertionError("No se pudo validar pgvector; revisar conexión y permisos. No se confirmaron cambios.") from None
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
