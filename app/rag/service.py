import json
import math

from openai import OpenAI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.core.config import settings


class PolicyReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_name: str
    page_number: int | None
    chunk_index: int
    content: str


def create_client():
    if not settings.openai_api_key or not settings.openai_api_key.get_secret_value():
        raise ValueError("Falta configuración de OpenAI")
    return OpenAI(api_key=settings.openai_api_key.get_secret_value(), timeout=90, max_retries=0)


def embed_texts(client, texts: list[str]) -> list[list[float]]:
    if not settings.openai_embedding_model:
        raise ValueError("Falta OPENAI_EMBEDDING_MODEL")
    response = client.embeddings.create(model=settings.openai_embedding_model, input=texts)
    records = sorted(response.data, key=lambda item: item.index)
    if [r.index for r in records] != list(range(len(texts))):
        raise ValueError("Embeddings incompletos")
    vectors = [r.embedding for r in records]
    if not vectors or not vectors[0] or any(
        len(v) != len(vectors[0]) or any(not math.isfinite(x) for x in v) or not any(v)
        for v in vectors
    ):
        raise ValueError("Embeddings no válidos")
    return vectors


RETRIEVAL_QUERY = text("""
    WITH compatible AS MATERIALIZED (
        SELECT * FROM app.policy_chunks
        WHERE embedding_model=:model AND vector_dims(embedding)=:dimensions
    )
    SELECT document_name, page_number, chunk_index, content FROM compatible
    ORDER BY embedding <=> CAST(:embedding AS vector), document_name, chunk_index
    LIMIT :limit
""")


def retrieve_policies(db, query: str, client=None, limit: int = 5) -> list[PolicyReference]:
    if not 1 <= limit <= 20:
        raise ValueError("Límite inválido")
    owned = client is None
    client = client or create_client()
    try:
        vector = embed_texts(client, [query])[0]
        rows = db.execute(RETRIEVAL_QUERY, {
            "model": settings.openai_embedding_model, "dimensions": len(vector),
            "embedding": json.dumps(vector, allow_nan=False), "limit": limit,
        }).mappings().all()
        if not rows:
            raise ValueError("Políticas no indexadas para el modelo configurado")
        return [PolicyReference.model_validate(dict(row)) for row in rows]
    finally:
        if owned:
            client.close()


MARGIN_POLICY_QUERY = (
    "Aumento de costos de productos, revisión del precio de venta ante cambios de costo, "
    "margen mínimo por línea y política de inventario y precios."
)
