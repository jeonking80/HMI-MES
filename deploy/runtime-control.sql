-- Operational on/off request table for the plant runtime host (apps/api/runtime_control.py).
-- Outside the versioned product schema; run once as the database owner. Idempotent.
CREATE SCHEMA IF NOT EXISTS runtime_ops AUTHORIZATION hmi_migrator;
CREATE TABLE IF NOT EXISTS runtime_ops.control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    desired text NOT NULL DEFAULT 'OFF' CHECK (desired IN ('ON', 'OFF')),
    requested_by text,
    requested_at timestamptz,
    run_until timestamptz,
    reported_state text CHECK (reported_state IN ('OFF', 'STARTING', 'RUNNING', 'STOPPING', 'ERROR')),
    reported_detail text CHECK (length(reported_detail) <= 200),
    reported_at timestamptz
);
ALTER TABLE runtime_ops.control OWNER TO hmi_migrator;
INSERT INTO runtime_ops.control (singleton) VALUES (true) ON CONFLICT DO NOTHING;
REVOKE ALL ON SCHEMA runtime_ops FROM PUBLIC;
REVOKE ALL ON runtime_ops.control FROM PUBLIC;
GRANT USAGE ON SCHEMA runtime_ops TO hmi_app;
GRANT SELECT, UPDATE ON runtime_ops.control TO hmi_app;
