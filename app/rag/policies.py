"""Indexación por documento: reemplazo atómico si cambia texto o modelo."""
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader

from app.core.config import settings
from app.rag.service import embed_texts


def extract_chunks(path: Path, size: int = 1200, overlap: int = 180) -> list[dict]:
    if not 0 <= overlap < size:
        raise ValueError("Tamaño de chunk inválido")
    chunks = []
    for number, page in enumerate(PdfReader(path).pages, 1):
        content = " ".join((page.extract_text() or "").split())
        for start in range(0, len(content), size-overlap):
            fragment = content[start:start+size]
            chunks.append({"page_number": number, "chunk_index": len(chunks),
                           "content": fragment, "content_hash": hashlib.sha256(fragment.encode()).hexdigest()})
            if start+size >= len(content):
                break
    if not chunks:
        raise ValueError("PDF sin texto extraíble; requiere OCR previo")
    return chunks


def index_document(connection, path: Path, client) -> int:
    chunks = extract_chunks(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # El lock serializa indexadores concurrentes del mismo documento.
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (path.name,))
        existing = connection.execute(
            "SELECT chunk_index, content_hash, document_hash, embedding_model FROM app.policy_chunks "
            "WHERE document_name=%s ORDER BY chunk_index", (path.name,)
        ).fetchall()
        expected = [(c["chunk_index"], c["content_hash"], digest, settings.openai_embedding_model) for c in chunks]
        if existing == expected:
            return 0
        vectors = []
        for start in range(0, len(chunks), 32):
            vectors.extend(embed_texts(client, [c["content"] for c in chunks[start:start+32]]))
        if len({len(v) for v in vectors}) != 1:
            raise ValueError("Dimensiones inconsistentes")
        connection.execute("DELETE FROM app.policy_chunks WHERE document_name=%s", (path.name,))
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO app.policy_chunks (document_name,page_number,chunk_index,content,embedding,"
                "embedding_model,document_hash,content_hash) VALUES (%s,%s,%s,%s,%s::vector,%s,%s,%s)",
                [(path.name,c["page_number"],c["chunk_index"],c["content"],json.dumps(v, allow_nan=False),
                  settings.openai_embedding_model,digest,c["content_hash"]) for c,v in zip(chunks,vectors,strict=True)],
            )
    return len(chunks)
