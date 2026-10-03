-- S3 Inventario: amplía el allowlist persistido de acciones sandbox.
-- Idempotente; no altera el dataset centinela ni filas existentes (es un superconjunto).
ALTER TABLE app.execution_actions DROP CONSTRAINT IF EXISTS execution_actions_action_type_check;
ALTER TABLE app.execution_actions ADD CONSTRAINT execution_actions_action_type_check CHECK (action_type IN (
    'CREATE_PRICE_REVIEW_DRAFT', 'CREATE_MARGIN_FOLLOWUP_TASK', 'CREATE_SUPPLIER_REVIEW_TASK',
    'CREATE_PURCHASE_ORDER_REVIEW_TASK', 'CREATE_SUPPLIER_FOLLOWUP_DRAFT',
    'CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK', 'CREATE_URGENT_PARTIAL_DELIVERY_REVIEW'
));
