-- CX-44: credentials unavailable in the diagnostic session; NOT EXECUTED.
-- Run twice at recorded UTC times; report elapsed seconds and per-table deltas.
-- No maintenance, DDL, ANALYZE, VACUUM or data writes.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '1s';
SELECT now() AS measured_at, pg_database_size(current_database()) AS database_bytes;
SELECT version_num FROM alembic_version;
SELECT s.relname, s.n_live_tup AS estimated_rows,
       pg_table_size(s.relid) AS heap_toast_bytes,
       pg_indexes_size(s.relid) AS index_bytes,
       pg_total_relation_size(s.relid) AS total_bytes,
       s.n_dead_tup AS estimated_dead_rows
FROM pg_stat_user_tables s WHERE s.schemaname='public' ORDER BY total_bytes DESC;
-- Exact counts can be generated with psycopg.sql.Identifier(table) and COUNT(*).
-- Do not present n_live_tup as exact COUNT(*); timeout tables stay unmeasured.
ROLLBACK;
