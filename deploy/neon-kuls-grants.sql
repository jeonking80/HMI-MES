-- Manual, reviewed post-migration capability grants. NEVER run at application startup.
-- Requires the dedicated hmi_mes_demo database already upgraded to 0017_guest_viewer.
-- Run the ENTIRE file as an operator allowed to SET ROLE hmi_migrator.
BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(843001);
-- Explicitly list pg_temp last; an implicit temporary schema otherwise precedes
-- relation lookups and could shadow catalogs during the preflight checks.
SET LOCAL search_path = pg_catalog, public, pg_temp;
DO $$ BEGIN
 IF current_database() <> 'hmi_mes_demo' THEN
  RAISE EXCEPTION 'Select the dedicated hmi_mes_demo database';
 END IF;
 IF (SELECT count(*) FROM public.alembic_version) <> 1
    OR (SELECT version_num FROM public.alembic_version) <> '0017_guest_viewer' THEN
  RAISE EXCEPTION 'Explicit migration to 0017_guest_viewer required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hmi_migrator')
    OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hmi_app') THEN
  RAISE EXCEPTION 'Existing dedicated migrator and runtime roles required';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hmi_app'
      AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
    OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member='hmi_app'::regrole)
    OR EXISTS(SELECT 1 FROM pg_class WHERE relowner='hmi_app'::regrole)
    OR EXISTS(SELECT 1 FROM pg_proc WHERE proowner='hmi_app'::regrole)
    OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspowner='hmi_app'::regrole)
    OR EXISTS(SELECT 1 FROM pg_database WHERE datdba='hmi_app'::regrole)
    OR has_schema_privilege('hmi_app','public','CREATE')
    OR has_database_privilege('hmi_app',current_database(),'CREATE,TEMPORARY') THEN
  RAISE EXCEPTION 'Runtime role must be nonprivileged, isolated and not an owner';
 END IF;
 IF (SELECT nspowner FROM pg_namespace WHERE nspname='public')
       IS DISTINCT FROM 'hmi_migrator'::regrole::oid THEN
  RAISE EXCEPTION 'Dedicated migrator schema ownership required';
 END IF;
 IF EXISTS(
   SELECT 1 FROM (VALUES
    ('public.analog_history_raw','r'),('public.analog_history_minutes','r'),
    ('public.analog_history_clock_state','r'),('public.downtime_reason_history','r'),
    ('public.guest_viewer_accounts','r'),('public.guest_rate_windows','r'),
    ('public.downtime_reason_history_id_seq','S')) AS expected(name,kind)
   LEFT JOIN pg_class c ON c.oid=to_regclass(expected.name)
   WHERE c.oid IS NULL OR c.relowner<>'hmi_migrator'::regrole::oid
     OR c.relkind::text<>expected.kind
 ) OR NOT EXISTS(
   SELECT 1 FROM pg_proc WHERE oid=to_regprocedure('public.immutable_downtime_reason()')
    AND proowner='hmi_migrator'::regrole::oid AND prorettype='trigger'::regtype
    AND NOT prosecdef
 ) THEN
  RAISE EXCEPTION 'Expected migration objects must be owned by hmi_migrator';
 END IF;
END $$;
SET LOCAL ROLE hmi_migrator;
-- Only these new objects are changed. Canonical14 grants are preserved.
REVOKE ALL ON TABLE public.analog_history_raw, public.analog_history_minutes,
 public.analog_history_clock_state, public.downtime_reason_history,
 public.guest_viewer_accounts, public.guest_rate_windows FROM PUBLIC, hmi_app;
REVOKE ALL ON SEQUENCE public.downtime_reason_history_id_seq FROM PUBLIC, hmi_app;
REVOKE ALL ON FUNCTION public.immutable_downtime_reason() FROM PUBLIC, hmi_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.analog_history_raw,
 public.analog_history_minutes, public.guest_rate_windows TO hmi_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.analog_history_clock_state TO hmi_app;
GRANT SELECT, INSERT ON TABLE public.downtime_reason_history TO hmi_app;
GRANT USAGE, SELECT ON SEQUENCE public.downtime_reason_history_id_seq TO hmi_app;
GRANT SELECT ON TABLE public.guest_viewer_accounts TO hmi_app;
GRANT EXECUTE ON FUNCTION public.immutable_downtime_reason() TO hmi_app;
COMMIT;
