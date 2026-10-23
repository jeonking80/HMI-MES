-- Manual, reviewed post-migration capability grants. NEVER run at application startup.
-- Run neon-kuls-grants.sql at head17 FIRST, then migrate to head18.
-- Requires the dedicated hmi_mes_demo database already upgraded to 0018_clock_evidence.
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
    OR (SELECT version_num FROM public.alembic_version) <> '0018_clock_evidence' THEN
  RAISE EXCEPTION 'Explicit migration to 0018_clock_evidence required';
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
    ('public.capture_clock_evidence','r'),('public.capture_clock_probes','r')) AS expected(name,kind)
   LEFT JOIN pg_class c ON c.oid=to_regclass(expected.name)
   WHERE c.oid IS NULL OR c.relowner<>'hmi_migrator'::regrole::oid
     OR c.relkind::text<>expected.kind
 ) OR NOT EXISTS(
   SELECT 1 FROM pg_proc WHERE oid=to_regprocedure('public.immutable_capture_clock_evidence()')
    AND proowner='hmi_migrator'::regrole::oid AND prorettype='trigger'::regtype
    AND NOT prosecdef
 ) THEN
  RAISE EXCEPTION 'Expected migration objects must be owned by hmi_migrator';
 END IF;
END $$;
SET LOCAL ROLE hmi_migrator;
-- Only schema18 objects are changed. Canonical14 and reviewed schema17 grants are preserved.
REVOKE ALL ON TABLE public.capture_clock_evidence, public.capture_clock_probes
 FROM PUBLIC, hmi_app;
REVOKE ALL ON FUNCTION public.immutable_capture_clock_evidence() FROM PUBLIC, hmi_app;
GRANT SELECT, INSERT ON TABLE public.capture_clock_evidence TO hmi_app;
GRANT SELECT, INSERT, DELETE ON TABLE public.capture_clock_probes TO hmi_app;
GRANT UPDATE (consumed_batch_id, consumed_input_sha256, consumed_at)
 ON TABLE public.capture_clock_probes TO hmi_app;
GRANT EXECUTE ON FUNCTION public.immutable_capture_clock_evidence() TO hmi_app;
COMMIT;
