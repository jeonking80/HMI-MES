-- Manual, reviewed post-migration capability grants. NEVER run at application startup.
-- Apply the reviewed schema17/18/19 grants at their exact revisions FIRST.
-- Requires the dedicated hmi_mes_demo database already upgraded to 0020_line_queue.
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
    OR (SELECT version_num FROM public.alembic_version) <> '0020_line_queue' THEN
  RAISE EXCEPTION 'Explicit migration to 0020_line_queue required';
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
    ('public.line_queue_policies','r'),('public.line_queue_entries','r'),
    ('public.line_queue_updates','r'),('public.line_queue_audit','r'),
    ('public.line_queue_audit_id_seq','S')) AS expected(name,kind)
   LEFT JOIN pg_class c ON c.oid=to_regclass(expected.name)
   WHERE c.oid IS NULL OR c.relowner<>'hmi_migrator'::regrole::oid
     OR c.relkind::text<>expected.kind
 ) OR NOT EXISTS(
   SELECT 1 FROM pg_proc WHERE oid=to_regprocedure('public.immutable_line_queue_evidence()')
    AND proowner='hmi_migrator'::regrole::oid AND prorettype='trigger'::regtype
    AND NOT prosecdef
 ) OR NOT EXISTS(
   SELECT 1 FROM pg_proc WHERE oid=to_regprocedure('public.preserve_line_queue_entry()')
    AND proowner='hmi_migrator'::regrole::oid AND prorettype='trigger'::regtype
    AND NOT prosecdef
 ) THEN
  RAISE EXCEPTION 'Expected migration objects must be owned by hmi_migrator';
 END IF;
END $$;
SET LOCAL ROLE hmi_migrator;
-- Only schema20 objects are changed; earlier capability grants are preserved.
REVOKE ALL ON TABLE public.line_queue_policies, public.line_queue_entries,
 public.line_queue_updates, public.line_queue_audit FROM PUBLIC, hmi_app;
REVOKE ALL ON SEQUENCE public.line_queue_audit_id_seq FROM PUBLIC, hmi_app;
REVOKE ALL ON FUNCTION public.immutable_line_queue_evidence() FROM PUBLIC, hmi_app;
REVOKE ALL ON FUNCTION public.preserve_line_queue_entry() FROM PUBLIC, hmi_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.line_queue_policies,
 public.line_queue_entries TO hmi_app;
GRANT SELECT, INSERT ON TABLE public.line_queue_updates, public.line_queue_audit TO hmi_app;
GRANT USAGE ON SEQUENCE public.line_queue_audit_id_seq TO hmi_app;
COMMIT;
