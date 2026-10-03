-- Core de aplicación. No modifica el dataset centinela ni el reloj simulado.
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    email text NOT NULL UNIQUE,
    role text NOT NULL CONSTRAINT users_role_check CHECK (role IN ('GERENTE', 'LIDER_PROCESO', 'ANALISTA', 'AUDITOR')),
    area text CONSTRAINT users_area_check CHECK (area IN ('COMERCIAL', 'CARTERA', 'COMPRAS', 'INVENTARIO')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO app.users (name, email, role, area) VALUES
    ('Gerente General', 'gerente@centinela.demo', 'GERENTE', NULL),
    ('Líder Comercial', 'lider.comercial@centinela.demo', 'LIDER_PROCESO', 'COMERCIAL'),
    ('Líder Cartera', 'lider.cartera@centinela.demo', 'LIDER_PROCESO', 'CARTERA'),
    ('Líder Compras', 'lider.compras@centinela.demo', 'LIDER_PROCESO', 'COMPRAS'),
    ('Líder Inventario', 'lider.inventario@centinela.demo', 'LIDER_PROCESO', 'INVENTARIO'),
    ('Analista', 'analista@centinela.demo', 'ANALISTA', NULL),
    ('Auditor', 'auditor@centinela.demo', 'AUDITOR', NULL)
ON CONFLICT (email) DO NOTHING;

CREATE TABLE IF NOT EXISTS app.alerts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type text NOT NULL,
    area text NOT NULL CONSTRAINT alerts_area_check CHECK (area IN ('COMERCIAL', 'CARTERA', 'COMPRAS', 'INVENTARIO')),
    title text NOT NULL,
    summary text NOT NULL,
    severity text NOT NULL,
    confidence numeric,
    amount_at_risk numeric(18,2),
    status text NOT NULL DEFAULT 'NEW' CONSTRAINT alerts_status_check CHECK (
        status IN ('NEW', 'ANALYZING', 'PROPOSED', 'APPROVED', 'REJECTED', 'EXECUTED', 'FAILED')
    ),
    simulated_date date NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    root_cause jsonb,
    proposals jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app.decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id uuid NOT NULL REFERENCES app.alerts(id),
    user_id uuid NOT NULL REFERENCES app.users(id),
    decision text NOT NULL CONSTRAINT decisions_decision_check CHECK (decision IN ('APPROVED', 'REJECTED', 'EDITED')),
    reason text,
    edited_proposal jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app.audit_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id uuid REFERENCES app.alerts(id),
    user_id uuid REFERENCES app.users(id),
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_alerts_simulated_date ON app.alerts (simulated_date);
CREATE INDEX IF NOT EXISTS ix_decisions_alert_id ON app.decisions (alert_id);
CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON app.audit_log (created_at);

-- También mantiene updated_at para futuras escrituras hechas directamente con SQL.
CREATE OR REPLACE FUNCTION app.touch_alert_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER alerts_updated_at
BEFORE UPDATE ON app.alerts
FOR EACH ROW EXECUTE FUNCTION app.touch_alert_updated_at();
