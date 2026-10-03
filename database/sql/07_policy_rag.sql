CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.policy_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_name text NOT NULL,
    page_number integer CHECK (page_number > 0),
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    content text NOT NULL,
    embedding vector NOT NULL,
    embedding_model text NOT NULL,
    document_hash text NOT NULL,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_name, chunk_index)
);
-- Dimensión variable: se valida y filtra por modelo/dimensión al recuperar.
-- Dataset pequeño: búsqueda exacta, sin índices vectoriales aproximados.
