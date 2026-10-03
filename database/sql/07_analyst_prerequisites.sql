-- Compatibilidad con workspaces donde el core anterior aún no está instalado.
-- Si las tablas ya existen, sus datos y definiciones se conservan.
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    email text NOT NULL UNIQUE,
    role text NOT NULL CHECK (role IN ('GERENTE', 'LIDER_PROCESO', 'ANALISTA', 'AUDITOR')),
    area text CHECK (area IN ('COMERCIAL', 'CARTERA', 'COMPRAS', 'INVENTARIO')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO app.users (name,email,role,area) VALUES
    ('Gerente General','gerente@centinela.demo','GERENTE',NULL),
    ('Líder Comercial','lider.comercial@centinela.demo','LIDER_PROCESO','COMERCIAL'),
    ('Líder Cartera','lider.cartera@centinela.demo','LIDER_PROCESO','CARTERA'),
    ('Líder Compras','lider.compras@centinela.demo','LIDER_PROCESO','COMPRAS'),
    ('Líder Inventario','lider.inventario@centinela.demo','LIDER_PROCESO','INVENTARIO'),
    ('Analista','analista@centinela.demo','ANALISTA',NULL),
    ('Auditor','auditor@centinela.demo','AUDITOR',NULL)
ON CONFLICT (email) DO NOTHING;
CREATE TABLE IF NOT EXISTS app.alerts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), type text NOT NULL,
    area text NOT NULL CHECK (area IN ('COMERCIAL','CARTERA','COMPRAS','INVENTARIO')),
    title text NOT NULL, summary text NOT NULL, severity text NOT NULL,
    confidence numeric, amount_at_risk numeric(18,2),
    status text NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW','ANALYZING','PROPOSED','APPROVED','REJECTED','EXECUTED','FAILED')),
    simulated_date date NOT NULL, detected_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb, root_cause jsonb, proposals jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS app.audit_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id uuid REFERENCES app.alerts(id), user_id uuid REFERENCES app.users(id),
    event_type text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
