import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.rag.policies import extract_chunks, index_document
from app.rag.service import MARGIN_POLICY_QUERY, embed_texts, retrieve_policies


class PolicyRagTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.model = patch.object(settings, "openai_embedding_model", "test-embedding")
        self.model.start()

    def tearDown(self):
        self.model.stop()

    def test_extracts_all_official_pdfs_with_page_provenance(self):
        directory = Path(__file__).resolve().parents[1] / "policies"
        files = sorted(directory.glob("*.pdf"))
        self.assertEqual(len(files), 3)
        for file in files:
            chunks = extract_chunks(file)
            self.assertEqual([c["chunk_index"] for c in chunks], list(range(len(chunks))))
            self.assertTrue(all(c["page_number"] >= 1 and 0 < len(c["content"]) <= 1200 for c in chunks))
            self.assertTrue(all(len(c["content_hash"]) == 64 for c in chunks))

    def test_retrieval_uses_pgvector_and_model_with_exact_provenance(self):
        self.client.embeddings.create.return_value.data = [SimpleNamespace(index=0, embedding=[.1, .2])]
        db = MagicMock()
        db.execute.return_value.mappings.return_value.all.return_value = [
            {"document_name": "policy.pdf", "page_number": 2, "chunk_index": 3, "content": "Texto recuperado"}
        ]
        result = retrieve_policies(db, MARGIN_POLICY_QUERY, client=self.client)
        self.assertEqual((result[0].document_name, result[0].page_number, result[0].chunk_index), ("policy.pdf", 2, 3))
        self.client.embeddings.create.assert_called_once_with(model="test-embedding", input=[MARGIN_POLICY_QUERY])
        query, params = db.execute.call_args.args
        self.assertIn("<=>", str(query))
        self.assertIn("MATERIALIZED", str(query))
        self.assertEqual(params["dimensions"], 2)
        self.assertEqual(params["model"], "test-embedding")
        self.assertNotIn("embedding", result[0].model_dump())

    def test_invalid_embedding_and_empty_index_fail_controlled(self):
        for vector in [[], [float("nan")], [0, 0]]:
            self.client.embeddings.create.return_value.data = [SimpleNamespace(index=0, embedding=vector)]
            with self.assertRaises(ValueError):
                embed_texts(self.client, ["texto"])
        self.client.embeddings.create.return_value.data = [SimpleNamespace(index=0, embedding=[1, 2])]
        db = MagicMock()
        db.execute.return_value.mappings.return_value.all.return_value = []
        with self.assertRaises(ValueError):
            retrieve_policies(db, "consulta", self.client)

    def test_index_skips_identical_chunks_and_replaces_changed_document_atomically(self):
        path = next((Path(__file__).resolve().parents[1] / "policies").glob("*.pdf"))
        chunks = extract_chunks(path)
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = [(c["chunk_index"], c["content_hash"], digest, "test-embedding") for c in chunks]
        db = MagicMock()
        db.transaction.side_effect = lambda: nullcontext()
        db.execute.return_value.fetchall.return_value = existing
        self.assertEqual(index_document(db, path, self.client), 0)
        self.client.embeddings.create.assert_not_called()
        self.assertFalse(any("DELETE" in str(c.args[0]) for c in db.execute.call_args_list))
        db.execute.return_value.fetchall.return_value = []
        self.client.embeddings.create.return_value.data = [SimpleNamespace(index=i, embedding=[1, 2]) for i in range(len(chunks))]
        self.assertEqual(index_document(db, path, self.client), len(chunks))
        db.cursor.return_value.__enter__.return_value.executemany.assert_called_once()

    def test_embedding_failure_never_deletes_previous_document(self):
        path = next((Path(__file__).resolve().parents[1] / "policies").glob("*.pdf"))
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = []
        self.client.embeddings.create.side_effect = RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            index_document(db, path, self.client)
        self.assertFalse(any("DELETE" in str(c.args[0]) for c in db.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
