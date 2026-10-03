-- S4: política estructurada y acciones sandbox. No modifica el dataset oficial.
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.discount_policy_limits (
    segmento text PRIMARY KEY,
    tope_especial_pct numeric(5,2) NOT NULL CHECK (tope_especial_pct >= 0 AND tope_especial_pct <= 100),
    document_name text NOT NULL
);

INSERT INTO app.discount_policy_limits (segmento, tope_especial_pct, document_name) VALUES
    ('Grandes superficies', 21, 'Politica_Descuentos_Comerciales.pdf'),
    ('Mayoristas', 18, 'Politica_Descuentos_Comerciales.pdf'),
    ('Minoristas', 13, 'Politica_Descuentos_Comerciales.pdf'),
    ('Institucional', 15, 'Politica_Descuentos_Comerciales.pdf')
ON CONFLICT (segmento) DO UPDATE SET
    tope_especial_pct = EXCLUDED.tope_especial_pct,
    document_name = EXCLUDED.document_name;

-- La tabla se crea en 08_executor.sql. Se conserva la allowlist en base de datos.
ALTER TABLE app.execution_actions
    DROP CONSTRAINT IF EXISTS execution_actions_action_type_check;

ALTER TABLE app.execution_actions
    ADD CONSTRAINT execution_actions_action_type_check CHECK (action_type IN (
        'CREATE_PRICE_REVIEW_DRAFT',
        'CREATE_MARGIN_FOLLOWUP_TASK',
        'CREATE_SUPPLIER_REVIEW_TASK',
        'CREATE_DISCOUNT_REVIEW_TASK',
        'CREATE_SELLER_COACHING_TASK',
        'CREATE_QUOTING_PERMISSION_REVIEW',
        'CREATE_COMMERCIAL_MANAGER_REVIEW'
    ));
