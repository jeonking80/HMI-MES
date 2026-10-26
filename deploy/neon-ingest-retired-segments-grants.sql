-- Manual, reviewed post-migration capability grants. NEVER run at application startup.
-- Apply the reviewed schema17/18/19/20/21/22/23 grants at their exact revisions FIRST.
-- Requires the dedicated hmi_mes_demo database already upgraded to 0024_ingest_retired_segments.
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
    OR (SELECT version_num FROM public.alembic_version) <> '0024_ingest_retired_segments' THEN
  RAISE EXCEPTION 'Explicit migration to 0024_ingest_retired_segments required';
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
 IF EXISTS(SELECT 1 FROM (VALUES
    ('public.ingest_receipts'),('public.ingest_retention_ranges'),
    ('public.ingest_retention_state'),('public.ingest_retired_batch_chunks'),
    ('public.ingest_retired_batch_segments')) expected(name)
    LEFT JOIN pg_class c ON c.oid=to_regclass(expected.name)
    WHERE c.oid IS NULL OR c.relowner<>'hmi_migrator'::regrole::oid OR c.relkind<>'r')
    OR EXISTS(SELECT 1 FROM (VALUES
      ('public.prune_ingest_batches(integer,integer,integer)'),
      ('public.prune_ingest_batches_hours(integer,integer,integer)'),
      ('public.compact_ingest_receipt(text,text)'),
      ('public.compact_ingest_receipts(text,text[],timestamptz)'),
      ('public.retired_ingest_batch_exists(text,text)'),
      ('public.ingest_receipt_referenced(text,text)'),
      ('public.guard_compacted_ingest_identity()'),
      ('public.prune_expired_clock_probes(timestamptz,text,text,text,numeric,text)'),
      ('public.capture_ingest_receipt()')) expected(name)
      LEFT JOIN pg_proc p ON p.oid=to_regprocedure(expected.name)
      WHERE p.oid IS NULL OR p.proowner<>'hmi_migrator'::regrole::oid OR NOT p.prosecdef)
    OR EXISTS(SELECT 1 FROM (VALUES
      ('public.guard_ingest_retention()'),('public.guard_retired_ingest_batch()'),
      ('public.guard_retired_ingest_segments()'),
      ('public.guard_consumed_clock_probe()'),('public.immutable_capture_clock_evidence()'))
      expected(name) LEFT JOIN pg_proc p ON p.oid=to_regprocedure(expected.name)
      WHERE p.oid IS NULL OR p.proowner<>'hmi_migrator'::regrole::oid OR p.prosecdef) THEN
  RAISE EXCEPTION 'Expected controlled retention objects and owners required';
 END IF;
END $$;
SET LOCAL ROLE hmi_migrator;
REVOKE ALL ON TABLE public.ingest_receipts,public.ingest_retention_ranges,
 public.ingest_retention_state,public.ingest_retired_batch_chunks,
 public.ingest_retired_batch_segments FROM PUBLIC,hmi_app;
GRANT SELECT ON TABLE public.ingest_receipts,public.ingest_retention_ranges,
 public.ingest_retention_state,public.ingest_retired_batch_chunks,
 public.ingest_retired_batch_segments TO hmi_app;
REVOKE ALL ON FUNCTION public.capture_ingest_receipt(),public.guard_ingest_retention(),
 public.guard_retired_ingest_batch(),public.guard_consumed_clock_probe(),
 public.retention_hash_add(text,text),public.retention_parameters_match(jsonb,jsonb,text),
 public.retention_canonical_json(jsonb),
 public.retention_clock_frontiers_merge(jsonb,jsonb),
 public.compact_ingest_receipt(text,text),public.ingest_receipt_referenced(text,text),
 public.guard_compacted_ingest_identity(),public.guard_retired_ingest_segments(),
 public.compact_ingest_receipts(text,text[],timestamptz),
 public.retired_ingest_batch_exists(text,text),
 public.prune_ingest_batches_hours(integer,integer,integer),
 public.prune_ingest_batches(integer,integer,integer),
 public.prune_expired_clock_probes(timestamptz,text,text,text,numeric,text) FROM PUBLIC,hmi_app;
GRANT EXECUTE ON FUNCTION public.retired_ingest_batch_exists(text,text),
 public.prune_ingest_batches_hours(integer,integer,integer),
 public.prune_expired_clock_probes(timestamptz,text,text,text,numeric,text) TO hmi_app;
REVOKE ALL ON SEQUENCE public.ingest_retired_batch_segments_segment_id_seq FROM PUBLIC,hmi_app;
REVOKE DELETE ON TABLE public.capture_clock_probes FROM PUBLIC,hmi_app;
COMMIT;
