CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.execution_actions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id uuid NOT NULL REFERENCES app.alerts(id),
    proposal_id text NOT NULL,
    action_type text NOT NULL CHECK (action_type IN (
        'CREATE_PRICE_REVIEW_DRAFT', 'CREATE_MARGIN_FOLLOWUP_TASK', 'CREATE_SUPPLIER_REVIEW_TASK'
    )),
    payload jsonb NOT NULL,
    result jsonb NOT NULL,
    created_by uuid NOT NULL REFERENCES app.users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    executed_at timestamptz NOT NULL DEFAULT now(),
    dedupe_key text NOT NULL UNIQUE,
    UNIQUE (alert_id, proposal_id)
);
