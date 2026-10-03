-- Deduplicación persistente de alertas. No altera el dataset oficial.
ALTER TABLE app.alerts ADD COLUMN IF NOT EXISTS dedupe_key text;
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_dedupe_key ON app.alerts (dedupe_key);
