"""Controlled raw retirement; compact UUID identities remain permanently retained.

Frozen SQL is also consumed directly by the HTTP renderer, without SQL splitting.
"""

from alembic import op

revision = "0022_ingest_retention"
down_revision = "0021_item_master"
branch_labels = None
depends_on = None

UPGRADE_SQL = (
    """CREATE TABLE public.ingest_receipts (
        gateway_id text NOT NULL, batch_id text NOT NULL,
        source_id text NOT NULL, stream_id text NOT NULL,
        capture_generation numeric(20,0) NOT NULL CHECK
            (capture_generation BETWEEN 0 AND 18446744073709551615),
        capture_seq numeric(20,0) NOT NULL CHECK
            (capture_seq BETWEEN 1 AND 18446744073709551615),
        content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
        stored_at timestamptz NOT NULL, captured_at timestamptz NOT NULL,
        clock_epoch text, monotonic_seconds double precision,
        utc_read_span_seconds double precision, expired_at timestamptz,
        PRIMARY KEY(gateway_id,batch_id),
        UNIQUE(gateway_id,capture_generation,capture_seq),
        UNIQUE(gateway_id,batch_id,source_id,stream_id,capture_generation,
            capture_seq,content_sha256),
        FOREIGN KEY(source_id,stream_id) REFERENCES public.source_streams
            (source_id,stream_id) ON DELETE RESTRICT
    )""",
    """INSERT INTO public.ingest_receipts SELECT gateway_id,batch_id,source_id,stream_id,
        capture_generation,capture_seq,content_sha256,stored_at,
        (body->>'captured_at')::timestamptz,body#>>'{capture_time,clock_epoch}',
        (body#>>'{capture_time,monotonic_seconds}')::double precision,
        (body#>>'{capture_time,utc_read_span_seconds}')::double precision,NULL
        FROM public.ingest_batches""",
    """CREATE INDEX ix_ingest_receipt_scope ON public.ingest_receipts
        (source_id,stream_id,gateway_id,capture_generation,capture_seq)""",
    """CREATE INDEX ix_ingest_receipt_clock ON public.ingest_receipts
        (gateway_id,source_id,stream_id,capture_generation,clock_epoch,capture_seq)""",
    """CREATE TABLE public.ingest_retention_ranges (
        line_id text NOT NULL REFERENCES public.lines(id) ON DELETE RESTRICT,
        gateway_id text NOT NULL, source_id text NOT NULL, stream_id text NOT NULL,
        capture_generation numeric(20,0) NOT NULL,
        from_capture_seq numeric(20,0) NOT NULL, through_capture_seq numeric(20,0) NOT NULL,
        deleted_count numeric(20,0) NOT NULL,
        hash_algorithm text NOT NULL CHECK(hash_algorithm='SHA256_SUM_MOD_2_256_V1'),
        hash_accumulator text NOT NULL CHECK(hash_accumulator ~ '^[0-9a-f]{64}$'),
        stored_from timestamptz NOT NULL, stored_until timestamptz NOT NULL,
        original_from timestamptz NOT NULL, original_until timestamptz NOT NULL,
        analytical_from timestamptz, analytical_until timestamptz,
        tag_ids text[] NOT NULL, clock_epochs text[] NOT NULL, time_unknown boolean NOT NULL,
        first_deleted_at timestamptz NOT NULL, last_deleted_at timestamptz NOT NULL,
        reason text NOT NULL CHECK(reason='RETENTION_EXPIRED'),
        PRIMARY KEY(gateway_id,source_id,stream_id,capture_generation,from_capture_seq),
        CHECK(from_capture_seq>=1 AND through_capture_seq<=18446744073709551615
            AND deleted_count=through_capture_seq-from_capture_seq+1 AND deleted_count>0),
        CHECK(stored_from<=stored_until AND original_from<=original_until)
    )""",
    """CREATE INDEX ix_ingest_retention_coverage ON public.ingest_retention_ranges
        (line_id,source_id,stream_id,analytical_from,analytical_until)""",
    """CREATE TABLE public.ingest_retention_state (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        last_attempt_at timestamptz, cursor_stored_at timestamptz,
        cursor_gateway_id text, cursor_batch_id text
    )""",
    "INSERT INTO public.ingest_retention_state(singleton) VALUES(true)",
    """CREATE INDEX ix_ingest_retention_candidates ON public.ingest_batches
        (stored_at,gateway_id,batch_id)""",
    """CREATE FUNCTION public.capture_ingest_receipt() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF TG_TABLE_SCHEMA<>'public' OR TG_TABLE_NAME<>'ingest_batches'
                OR TG_OP<>'INSERT' THEN RAISE EXCEPTION 'invalid receipt trigger'; END IF;
            INSERT INTO public.ingest_receipts VALUES(NEW.gateway_id,NEW.batch_id,
                NEW.source_id,NEW.stream_id,NEW.capture_generation,NEW.capture_seq,
                NEW.content_sha256,NEW.stored_at,(NEW.body->>'captured_at')::timestamptz,
                NEW.body#>>'{capture_time,clock_epoch}',
                (NEW.body#>>'{capture_time,monotonic_seconds}')::double precision,
                (NEW.body#>>'{capture_time,utc_read_span_seconds}')::double precision,NULL);
            RETURN NEW;
        END $$""",
    """CREATE TRIGGER capture_ingest_receipt AFTER INSERT ON public.ingest_batches
        FOR EACH ROW EXECUTE FUNCTION public.capture_ingest_receipt()""",
    """DO $$ DECLARE fk text; BEGIN
        SELECT conname INTO STRICT fk FROM pg_constraint
        WHERE conrelid='public.alarm_capture_decisions'::regclass
            AND confrelid='public.ingest_batches'::regclass AND contype='f';
        EXECUTE format('ALTER TABLE public.alarm_capture_decisions DROP CONSTRAINT %I',fk);
        EXECUTE format('ALTER TABLE public.alarm_capture_decisions ADD CONSTRAINT %I
            FOREIGN KEY(gateway_id,batch_id,source_id,stream_id,capture_generation,
            capture_seq,content_sha256) REFERENCES public.ingest_receipts(gateway_id,
            batch_id,source_id,stream_id,capture_generation,capture_seq,content_sha256)
            ON DELETE RESTRICT',fk);
    END $$""",
    """CREATE FUNCTION public.guard_ingest_retention() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF TG_TABLE_NAME='ingest_receipts' AND TG_OP='UPDATE' THEN
                IF current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class
                    WHERE oid='public.ingest_receipts'::regclass)
                AND OLD.expired_at IS NULL AND NEW.expired_at IS NOT NULL
                AND (to_jsonb(NEW)-'expired_at')=(to_jsonb(OLD)-'expired_at')
                AND EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                    WHERE r.gateway_id=OLD.gateway_id AND r.source_id=OLD.source_id
                    AND r.stream_id=OLD.stream_id
                    AND r.capture_generation=OLD.capture_generation
                    AND OLD.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq)
                THEN RETURN NEW; END IF;
            END IF;
            IF TG_TABLE_NAME IN ('ingest_retention_ranges','ingest_retention_state')
                AND TG_OP<>'TRUNCATE'
                AND current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class
                    WHERE oid='public.ingest_receipts'::regclass)
            THEN
                IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Retention identity and evidence are immutable';
        END $$""",
    """CREATE OR REPLACE FUNCTION public.immutable_capture_clock_evidence() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                IF current_user=(SELECT pg_get_userbyid(relowner)
                FROM pg_class WHERE oid='public.ingest_receipts'::regclass)
                AND EXISTS(SELECT 1 FROM public.ingest_receipts p
                    JOIN public.ingest_retention_ranges r ON r.gateway_id=p.gateway_id
                    AND r.source_id=p.source_id AND r.stream_id=p.stream_id
                    AND r.capture_generation=p.capture_generation
                    AND p.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq
                    WHERE p.gateway_id=OLD.gateway_id AND p.batch_id=OLD.batch_id
                    AND p.source_id=OLD.source_id AND p.stream_id=OLD.stream_id
                    AND p.capture_generation=OLD.capture_generation
                    AND p.capture_seq=OLD.capture_seq AND p.content_sha256=OLD.batch_sha256
                    AND p.expired_at IS NOT NULL AND r.reason='RETENTION_EXPIRED')
                THEN RETURN OLD; END IF;
            END IF;
            RAISE EXCEPTION 'Capture clock evidence is immutable';
        END $$""",
    """CREATE FUNCTION public.retention_hash_add(a text,b text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER
        SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE n numeric:=0; m numeric:=0; v numeric; result text:=''; i integer;
        BEGIN
            FOR i IN 0..31 LOOP
                n:=n*256+get_byte(decode(a,'hex'),i);
                m:=m*256+get_byte(decode(b,'hex'),i);
            END LOOP;
            v:=mod(n+m,
                115792089237316195423570985008687907853269984665640564039457584007913129639936);
            FOR i IN 1..32 LOOP
                result:=lpad(to_hex(mod(v,256)::integer),2,'0')||result;
                v:=pg_catalog.div(v,256);
            END LOOP;
            RETURN result;
        END $$""",
    """CREATE FUNCTION public.prune_expired_clock_probes(cutoff timestamptz,
        scope_gateway text DEFAULT NULL,scope_source text DEFAULT NULL,
        scope_stream text DEFAULT NULL,scope_generation numeric DEFAULT NULL,
        scope_epoch text DEFAULT NULL) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE removed integer; bounded_cutoff timestamptz;
        BEGIN
            IF cutoff IS NULL THEN RAISE EXCEPTION 'Invalid probe cleanup cutoff'; END IF;
            bounded_cutoff:=least(cutoff,clock_timestamp());
            IF scope_gateway IS NULL AND scope_source IS NULL AND scope_stream IS NULL
                AND scope_generation IS NULL AND scope_epoch IS NULL THEN
                DELETE FROM public.capture_clock_probes WHERE nonce_sha256 IN
                    (SELECT nonce_sha256 FROM public.capture_clock_probes
                     WHERE consumed_batch_id IS NULL AND expires_at<bounded_cutoff
                     ORDER BY expires_at LIMIT 400 FOR UPDATE SKIP LOCKED);
            ELSE
                IF scope_gateway IS NULL OR scope_source IS NULL OR scope_stream IS NULL
                    OR scope_generation IS NULL OR scope_epoch IS NULL THEN
                    RAISE EXCEPTION 'Complete probe cleanup scope required'; END IF;
                WITH preferred AS MATERIALIZED (
                    SELECT nonce_sha256,expires_at,0 AS priority
                    FROM public.capture_clock_probes
                    WHERE consumed_batch_id IS NULL AND expires_at<bounded_cutoff
                        AND gateway_id=scope_gateway AND source_id=scope_source
                        AND stream_id=scope_stream AND capture_generation=scope_generation
                        AND clock_epoch=scope_epoch
                    ORDER BY expires_at LIMIT 100 FOR UPDATE SKIP LOCKED
                ), fallback AS MATERIALIZED (
                    SELECT nonce_sha256,expires_at,1 AS priority
                    FROM public.capture_clock_probes
                    WHERE consumed_batch_id IS NULL AND expires_at<bounded_cutoff
                    ORDER BY expires_at LIMIT 100 FOR UPDATE SKIP LOCKED
                ), candidates AS (
                    SELECT * FROM preferred UNION ALL SELECT * FROM fallback
                )
                DELETE FROM public.capture_clock_probes WHERE nonce_sha256 IN (
                    SELECT nonce_sha256 FROM candidates GROUP BY nonce_sha256
                    ORDER BY min(priority),min(expires_at),nonce_sha256 LIMIT 100);
            END IF;
            GET DIAGNOSTICS removed=ROW_COUNT;
            RETURN removed;
        END $$""",
)

_PRUNE_SQL = """CREATE FUNCTION public.prune_ingest_batches(
    retention_days integer,max_batches integer,max_candidates integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE
        tick timestamptz:=clock_timestamp(); st public.ingest_retention_state%ROWTYPE;
        b record; s public.source_streams%ROWTYPE; ev jsonb; valid boolean;
        chosen boolean:=false; budget_exhausted boolean:=false;
        scanned integer:=0; removed integer:=0; key bigint;
        leaf text; lo timestamptz; hi timestamptz; alo timestamptz; ahi timestamptz;
        tags text[]; epochs text[]; unknown_time boolean; adjacent record;
        range_start numeric; range_end numeric; amount numeric; accumulator text;
        first_at timestamptz; slo timestamptz; shi timestamptz;
    BEGIN
        IF retention_days IS NULL OR retention_days<1 OR max_batches IS NULL
            OR max_batches<1 OR max_batches>1000 OR max_candidates IS NULL
            OR max_candidates<1 OR max_candidates>2000 THEN
            RAISE EXCEPTION 'Invalid bounded retention parameters'; END IF;
        IF NOT pg_try_advisory_xact_lock_shared(843001) THEN
            RETURN jsonb_build_object('status','BUSY','deleted_count',0,
                'scanned_count',0,'budget_exhausted',false);
        END IF;
        SELECT * INTO st FROM public.ingest_retention_state WHERE singleton
            FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('status','BUSY','deleted_count',0,
                'scanned_count',0,'budget_exhausted',false);
        END IF;
        IF st.last_attempt_at>tick-interval '1 minute' THEN
            RETURN jsonb_build_object('status','THROTTLED','deleted_count',0,
                'scanned_count',0,'budget_exhausted',false);
        END IF;
        UPDATE public.ingest_retention_state SET last_attempt_at=tick WHERE singleton;
        FOR b IN SELECT ib.* FROM public.ingest_batches ib
            WHERE ib.stored_at<tick-make_interval(days=>retention_days)
            AND (st.cursor_stored_at IS NULL OR
                ROW(ib.stored_at,ib.gateway_id,ib.batch_id)>
                ROW(st.cursor_stored_at,st.cursor_gateway_id,st.cursor_batch_id))
            ORDER BY ib.stored_at,ib.gateway_id,ib.batch_id LIMIT max_candidates
        LOOP
            IF clock_timestamp()-tick>=interval '500 milliseconds' THEN
                budget_exhausted:=true;
                EXIT;
            END IF;
            scanned:=scanned+1;
            UPDATE public.ingest_retention_state SET cursor_stored_at=b.stored_at,
                cursor_gateway_id=b.gateway_id,cursor_batch_id=b.batch_id WHERE singleton;
            BEGIN
            IF NOT chosen THEN
                key:=('x'||substr(encode(sha256(convert_to('gateway:'||b.gateway_id,
                    'UTF8')),'hex'),1,16))::bit(64)::bigint;
                IF NOT pg_try_advisory_xact_lock(key) THEN RAISE SQLSTATE 'P1001'; END IF;
                BEGIN
                    SELECT * INTO s FROM public.source_streams
                        WHERE source_id=b.source_id AND stream_id=b.stream_id;
                    IF NOT FOUND THEN RAISE SQLSTATE 'P1001'; END IF;
                    PERFORM 1 FROM public.lines WHERE id=s.line_id FOR UPDATE NOWAIT;
                    SELECT * INTO s FROM public.source_streams
                        WHERE source_id=b.source_id AND stream_id=b.stream_id FOR UPDATE NOWAIT;
                EXCEPTION WHEN lock_not_available THEN RAISE SQLSTATE 'P1001';
                END;
            END IF;
            IF b.gateway_id<>s.gateway_id OR b.source_id<>s.source_id
                OR b.stream_id<>s.stream_id OR b.capture_generation>s.capture_generation
                OR s.conflicted OR s.reconciliation_required THEN RAISE SQLSTATE 'P1001'; END IF;
            IF (jsonb_array_length(b.body->'events')=0 AND (
                EXISTS(SELECT 1 FROM public.stream_gaps
                WHERE source_id=s.source_id AND stream_id=s.stream_id)
                OR EXISTS(SELECT 1 FROM public.event_inbox WHERE source_id=s.source_id
                    AND stream_id=s.stream_id AND applied_at IS NULL)
                OR EXISTS(SELECT 1 FROM public.command_requests WHERE source_id=s.source_id
                    AND stream_id=s.stream_id AND (collision_latched OR status IN
                    ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')))
                )) OR jsonb_array_length(COALESCE(b.body->'gaps','[]'::jsonb))>0
            THEN RAISE SQLSTATE 'P1001'; END IF;
            valid:=true;
            FOR ev IN SELECT value FROM jsonb_array_elements(b.body->'events') LOOP
                IF NOT EXISTS(SELECT 1 FROM public.event_inbox i
                    JOIN public.production_executions x ON x.execution_id=i.execution_id
                    JOIN public.run_reconciliations r ON r.execution_id=x.execution_id
                    JOIN public.execution_totals t ON t.execution_id=x.execution_id
                    JOIN public.stream_progress p ON p.source_id=i.source_id
                        AND p.stream_id=i.stream_id
                    WHERE i.event_id=ev->>'event_id' AND i.body=ev
                    AND i.source_id=s.source_id AND i.stream_id=s.stream_id
                    AND i.seq=(ev->>'seq')::numeric AND i.kind=ev->>'kind'
                    AND i.execution_id=ev->>'execution_id' AND i.applied_at IS NOT NULL
                    AND i.reason_code IS NULL AND i.reason IS NULL
                    AND x.source_id=s.source_id AND x.stream_id=s.stream_id
                    AND r.status='SETTLED' AND r.final_summary->>'final'='true'
                    AND r.final_summary->>'state'='ENDED'
                    AND r.final_summary->>'execution_id'=x.execution_id
                    AND r.final_summary->>'source_id'=s.source_id
                    AND r.final_summary->>'stream_id'=s.stream_id
                    AND r.final_summary->>'recipe_version'=x.recipe_version
                    AND r.final_summary->>'counter_epoch'=x.counter_epoch
                    AND public.retention_parameters_match(r.final_summary,x.parameters,
                        x.recipe_version)
                    AND (r.final_summary->>'wip_quantity')::numeric=0
                    AND r.final_summary->>'ended_at' IS NOT NULL
                    AND (r.final_summary->>'good_quantity')::numeric=t.good_quantity
                    AND (r.final_summary->>'reject_quantity')::numeric=t.reject_quantity
                    AND (r.final_summary->>'scrap_quantity')::numeric=t.scrap_quantity
                    AND (r.final_summary->>'input_quantity')::numeric=
                        t.good_quantity+t.reject_quantity+t.scrap_quantity
                    AND p.verified_through_seq>=(r.final_summary->>'last_event_seq')::numeric
                    AND i.seq<=p.verified_through_seq
                    AND NOT EXISTS(SELECT 1 FROM public.stream_gaps g
                        WHERE g.source_id=s.source_id AND g.stream_id=s.stream_id
                        AND g.from_seq<=(r.final_summary->>'last_event_seq')::numeric)
                    AND NOT EXISTS(SELECT 1 FROM public.event_inbox q
                        WHERE q.source_id=s.source_id AND q.stream_id=s.stream_id
                        AND q.seq<=(r.final_summary->>'last_event_seq')::numeric
                        AND q.applied_at IS NULL)
                    AND NOT EXISTS(SELECT 1 FROM public.command_requests c
                        WHERE (c.execution_id=x.execution_id OR (c.execution_id IS NULL
                            AND c.source_id=s.source_id AND c.stream_id=s.stream_id))
                        AND (c.collision_latched OR
                        c.status IN ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')))
                    AND NOT EXISTS(SELECT 1 FROM public.event_inbox later
                        WHERE later.source_id=s.source_id AND later.stream_id=s.stream_id
                        AND later.execution_id=x.execution_id
                        AND later.seq>(r.final_summary->>'last_event_seq')::numeric
                        AND (later.applied_at IS NULL OR later.seq>p.verified_through_seq
                            OR later.kind<>'STATE_CHANGED'
                            OR COALESCE(later.body#>>'{payload,previous_state}','')
                                NOT IN ('STOPPED','RECOVERY_REQUIRED')
                            OR COALESCE(later.body#>>'{payload,state}','')
                                NOT IN ('STOPPED','RECOVERY_REQUIRED')))
                    AND COALESCE((SELECT last_event.body#>>'{payload,state}'
                        FROM public.event_inbox last_event
                        WHERE last_event.source_id=s.source_id
                        AND last_event.stream_id=s.stream_id
                        AND last_event.execution_id=x.execution_id
                        AND last_event.seq>(r.final_summary->>'last_event_seq')::numeric
                        ORDER BY last_event.seq DESC LIMIT 1),'STOPPED')='STOPPED'
                    AND NOT EXISTS(SELECT 1 FROM public.run_summary_conflicts c
                        WHERE c.execution_id=x.execution_id)) THEN valid:=false; EXIT; END IF;
            END LOOP;
            IF NOT valid THEN RAISE SQLSTATE 'P1001'; END IF;
            PERFORM 1 FROM public.ingest_batches WHERE gateway_id=b.gateway_id
                AND batch_id=b.batch_id FOR UPDATE SKIP LOCKED;
            IF NOT FOUND THEN RAISE SQLSTATE 'P1001'; END IF;
            chosen:=true;
            leaf:=encode(sha256(convert_to(jsonb_build_array('HMI_RETENTION_LEAF_V1',
                b.gateway_id,b.source_id,b.stream_id,b.capture_generation::text,
                b.capture_seq::text,b.batch_id,b.content_sha256)::text,'UTF8')),'hex');
            SELECT min(v),max(v) INTO lo,hi FROM (
                SELECT (b.body->>'captured_at')::timestamptz AS v
                UNION ALL SELECT (value->>'acquired_at')::timestamptz
                    FROM jsonb_array_elements(b.body->'samples')) times;
            SELECT c.status<>'GOOD',c.canonical_api_utc+(lo-c.original_capture_utc),
                c.canonical_api_utc+(hi-c.original_capture_utc)
                INTO unknown_time,alo,ahi FROM public.capture_clock_evidence c
                WHERE c.gateway_id=b.gateway_id AND c.batch_id=b.batch_id;
            unknown_time:=COALESCE(unknown_time,true) OR EXISTS(
                SELECT 1 FROM public.analog_history_clock_state cs
                JOIN public.ingest_receipts receipt ON receipt.gateway_id=b.gateway_id
                    AND receipt.batch_id=b.batch_id
                WHERE cs.gateway_id=b.gateway_id AND cs.source_id=b.source_id
                    AND cs.stream_id=b.stream_id
                    AND cs.capture_generation=b.capture_generation
                    AND cs.clock_epoch=receipt.clock_epoch
                    AND cs.reversal_seq IS NOT NULL AND b.capture_seq>=cs.reversal_seq);
            IF unknown_time THEN alo:=NULL; ahi:=NULL; END IF;
            SELECT COALESCE(array_agg(DISTINCT value->>'tag_id'),'{}'::text[]) INTO tags
                FROM jsonb_array_elements(b.body->'samples');
            epochs:=ARRAY(SELECT clock_epoch FROM public.ingest_receipts
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id
                    AND clock_epoch IS NOT NULL);
            range_start:=b.capture_seq; range_end:=b.capture_seq; amount:=1;
            accumulator:=leaf; first_at:=tick; slo:=b.stored_at; shi:=b.stored_at;
            FOR adjacent IN SELECT * FROM public.ingest_retention_ranges r
                WHERE r.gateway_id=b.gateway_id AND r.source_id=b.source_id
                AND r.stream_id=b.stream_id AND r.capture_generation=b.capture_generation
                AND (r.through_capture_seq=b.capture_seq-1
                    OR r.from_capture_seq=b.capture_seq+1) FOR UPDATE
            LOOP
                range_start:=least(range_start,adjacent.from_capture_seq);
                range_end:=greatest(range_end,adjacent.through_capture_seq);
                amount:=amount+adjacent.deleted_count;
                accumulator:=public.retention_hash_add(accumulator,adjacent.hash_accumulator);
                first_at:=least(first_at,adjacent.first_deleted_at);
                slo:=least(slo,adjacent.stored_from); shi:=greatest(shi,adjacent.stored_until);
                lo:=least(lo,adjacent.original_from); hi:=greatest(hi,adjacent.original_until);
                alo:=least(alo,adjacent.analytical_from);
                ahi:=greatest(ahi,adjacent.analytical_until);
                unknown_time:=unknown_time OR adjacent.time_unknown;
                tags:=ARRAY(SELECT DISTINCT unnest(tags||adjacent.tag_ids) ORDER BY 1);
                epochs:=ARRAY(SELECT DISTINCT unnest(epochs||adjacent.clock_epochs) ORDER BY 1);
                DELETE FROM public.ingest_retention_ranges
                    WHERE gateway_id=adjacent.gateway_id AND source_id=adjacent.source_id
                    AND stream_id=adjacent.stream_id
                    AND capture_generation=adjacent.capture_generation
                    AND from_capture_seq=adjacent.from_capture_seq;
            END LOOP;
            INSERT INTO public.ingest_retention_ranges VALUES(s.line_id,b.gateway_id,
                b.source_id,b.stream_id,b.capture_generation,range_start,range_end,amount,
                'SHA256_SUM_MOD_2_256_V1',accumulator,slo,shi,lo,hi,alo,ahi,tags,epochs,
                unknown_time,first_at,tick,'RETENTION_EXPIRED');
            UPDATE public.ingest_receipts SET expired_at=tick
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            DELETE FROM public.capture_clock_probes p WHERE p.gateway_id=b.gateway_id
                AND p.source_id=b.source_id AND p.stream_id=b.stream_id
                AND p.capture_generation=b.capture_generation AND p.consumed_batch_id=b.batch_id
                AND EXISTS(SELECT 1 FROM public.capture_clock_evidence c
                    WHERE c.gateway_id=b.gateway_id AND c.batch_id=b.batch_id
                    AND c.input_evidence_sha256=p.consumed_input_sha256);
            DELETE FROM public.capture_clock_evidence
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            DELETE FROM public.ingest_batches WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            removed:=removed+1;
            IF removed>=max_batches THEN EXIT; END IF;
            EXCEPTION WHEN SQLSTATE 'P1001' THEN NULL;
            END;
        END LOOP;
        IF NOT budget_exhausted AND scanned<max_candidates AND removed<max_batches THEN
            UPDATE public.ingest_retention_state SET cursor_stored_at=NULL,
                cursor_gateway_id=NULL,cursor_batch_id=NULL WHERE singleton;
        END IF;
        RETURN jsonb_build_object('status',CASE WHEN removed>0 THEN 'PRUNED' ELSE 'EMPTY' END,
            'deleted_count',removed,'scanned_count',scanned,'budget_exhausted',budget_exhausted);
    END $$"""


def upgrade_statements():
    statements = []
    for statement in UPGRADE_SQL:
        statements.append(statement)
    for table in ("ingest_receipts", "ingest_retention_ranges", "ingest_retention_state"):
        statements.append(f"""CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.guard_ingest_retention()""")
        statements.append(f"""CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON public.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION public.guard_ingest_retention()""")
    statements.append("""CREATE FUNCTION public.guard_retired_ingest_batch() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                IF current_user=(SELECT pg_get_userbyid(relowner)
                FROM pg_class WHERE oid='public.ingest_receipts'::regclass)
                AND EXISTS(SELECT 1 FROM public.ingest_receipts p
                    JOIN public.ingest_retention_ranges r ON r.gateway_id=p.gateway_id
                    AND r.source_id=p.source_id AND r.stream_id=p.stream_id
                    AND r.capture_generation=p.capture_generation
                    AND p.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq
                    WHERE p.gateway_id=OLD.gateway_id AND p.batch_id=OLD.batch_id
                    AND p.source_id=OLD.source_id AND p.stream_id=OLD.stream_id
                    AND p.capture_generation=OLD.capture_generation
                    AND p.capture_seq=OLD.capture_seq
                    AND p.content_sha256=OLD.content_sha256 AND p.expired_at IS NOT NULL)
                THEN RETURN OLD; END IF;
            END IF;
            RAISE EXCEPTION 'Ingest batch evidence is immutable';
        END $$""")
    statements.append("""CREATE TRIGGER ingest_batch_retention_guard BEFORE UPDATE OR DELETE
        ON public.ingest_batches FOR EACH ROW
        EXECUTE FUNCTION public.guard_retired_ingest_batch()""")
    statements.append("""CREATE TRIGGER ingest_batch_retention_no_truncate BEFORE TRUNCATE
        ON public.ingest_batches FOR EACH STATEMENT
        EXECUTE FUNCTION public.guard_retired_ingest_batch()""")
    statements.append("""CREATE FUNCTION public.guard_consumed_clock_probe() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF OLD.consumed_batch_id IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Consumed clock probe is immutable'; END IF;
            RETURN NEW;
        END $$""")
    statements.append("""CREATE TRIGGER consumed_clock_probe_guard BEFORE UPDATE
        ON public.capture_clock_probes FOR EACH ROW
        EXECUTE FUNCTION public.guard_consumed_clock_probe()""")
    statements.append("""CREATE FUNCTION public.retention_canonical_json(value jsonb) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER
        SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE result text;
        BEGIN
            IF jsonb_typeof(value)='object' THEN
                SELECT '{'||COALESCE(string_agg(to_jsonb(k)::text||':'||
                    public.retention_canonical_json(v),',' ORDER BY k COLLATE "C"),'')||'}'
                    INTO result FROM jsonb_each(value) entries(k,v);
            ELSIF jsonb_typeof(value)='array' THEN
                SELECT '['||COALESCE(string_agg(public.retention_canonical_json(v),','
                    ORDER BY n),'')||']' INTO result
                    FROM jsonb_array_elements(value) WITH ORDINALITY entries(v,n);
            ELSE result:=value::text;
            END IF;
            RETURN result;
        END $$""")
    statements.append("""CREATE FUNCTION public.retention_parameters_match(
        summary jsonb,baseline jsonb,recipe text) RETURNS boolean
        LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE change jsonb; previous jsonb:=baseline; previous_seq numeric:=0;
            previous_version numeric:=-1; previous_time timestamptz;
            seen_ids text[]:='{}'; seen_commands text[]:='{}'; durable jsonb;
            changes jsonb:=COALESCE(summary->'parameter_changes','[]'::jsonb);
            base_speed numeric:=(baseline->>'speed_m_per_min')::numeric;
            speed numeric;
        BEGIN
            IF summary->>'recipe_version' IS DISTINCT FROM recipe
                OR (summary->'baseline_parameters' IS NOT NULL
                    AND summary->'baseline_parameters'<>baseline) THEN RETURN false; END IF;
            SELECT COALESCE(jsonb_agg(i.body ORDER BY i.seq),'[]'::jsonb) INTO durable
                FROM public.event_inbox i
                WHERE i.source_id=summary->>'source_id' AND i.stream_id=summary->>'stream_id'
                AND i.seq<=(summary->>'last_event_seq')::numeric
                AND (i.execution_id=summary->>'execution_id'
                    OR i.body->>'execution_id'=summary->>'execution_id')
                AND (i.kind='PARAMETERS_CHANGED' OR i.body->>'kind'='PARAMETERS_CHANGED');
            IF durable<>changes THEN RETURN false; END IF;
            IF jsonb_array_length(changes)>0
                AND summary->'baseline_parameters' IS DISTINCT FROM baseline
            THEN RETURN false; END IF;
            FOR change IN SELECT value FROM jsonb_array_elements(changes) LOOP
                IF NOT EXISTS(SELECT 1 FROM public.event_inbox i
                    WHERE i.event_id=change->>'event_id' AND i.body=change
                    AND i.execution_id=summary->>'execution_id'
                    AND i.source_id=summary->>'source_id' AND i.stream_id=summary->>'stream_id'
                    AND i.kind='PARAMETERS_CHANGED' AND i.seq=(change->>'seq')::numeric
                    AND i.content_sha256=encode(sha256(convert_to(
                        public.retention_canonical_json(change),'UTF8')),'hex')
                    AND i.applied_at IS NOT NULL AND i.reason_code IS NULL)
                    OR change->>'equipment_id'<>'Line'
                    OR change#>>'{payload,recipe_version}' IS DISTINCT FROM recipe
                    OR change#>'{payload,baseline_parameters}' IS DISTINCT FROM baseline
                    OR change#>'{payload,previous_parameters}' IS DISTINCT FROM previous
                    OR (change->>'seq')::numeric<=previous_seq
                    OR (change#>>'{payload,control_version}')::numeric<=previous_version
                    OR (change->>'occurred_at')::timestamptz>
                        (summary->>'ended_at')::timestamptz
                    OR (change->>'occurred_at')::timestamptz<previous_time
                    OR change->>'event_id'=ANY(seen_ids)
                    OR change#>>'{payload,command_id}'=ANY(seen_commands)
                THEN RETURN false; END IF;
                previous:=change#>'{payload,parameters}';
                speed:=(previous->>'speed_m_per_min')::numeric;
                IF previous->'temperature_c' IS DISTINCT FROM baseline->'temperature_c'
                    OR speed<1 OR speed>60 OR speed<base_speed*0.8 OR speed>base_speed*1.2
                THEN RETURN false; END IF;
                previous_seq:=(change->>'seq')::numeric;
                previous_version:=(change#>>'{payload,control_version}')::numeric;
                previous_time:=(change->>'occurred_at')::timestamptz;
                seen_ids:=array_append(seen_ids,change->>'event_id');
                seen_commands:=array_append(seen_commands,change#>>'{payload,command_id}');
            END LOOP;
            RETURN previous=summary->'parameters';
        END $$""")
    statements.append(_PRUNE_SQL)
    for function in (
        "capture_ingest_receipt()",
        "guard_retired_ingest_batch()",
        "guard_consumed_clock_probe()",
        "guard_ingest_retention()",
        "retention_hash_add(text,text)",
        "retention_parameters_match(jsonb,jsonb,text)",
        "retention_canonical_json(jsonb)",
        "prune_ingest_batches(integer,integer,integer)",
        "prune_expired_clock_probes(timestamptz,text,text,text,numeric,text)",
    ):
        statements.append(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    statements.append("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hmi_app') THEN
        REVOKE DELETE ON public.capture_clock_probes FROM hmi_app;
    END IF; END $$""")

    return tuple(statements)


def upgrade():
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.ingest_retention_ranges)
            OR EXISTS(SELECT 1 FROM public.ingest_receipts WHERE expired_at IS NOT NULL)
            OR EXISTS(SELECT 1 FROM public.ingest_receipts p
                LEFT JOIN public.ingest_batches b USING(gateway_id,batch_id)
                WHERE b.batch_id IS NULL) THEN
            RAISE EXCEPTION 'Retired ingestion evidence cannot be downgraded'; END IF;
    END $$""")
    op.execute("""DO $$ DECLARE fk text; BEGIN
        SELECT conname INTO STRICT fk FROM pg_constraint
        WHERE conrelid='public.alarm_capture_decisions'::regclass
            AND confrelid='public.ingest_receipts'::regclass AND contype='f';
        EXECUTE format('ALTER TABLE public.alarm_capture_decisions DROP CONSTRAINT %I',fk);
        EXECUTE format('ALTER TABLE public.alarm_capture_decisions ADD CONSTRAINT %I
            FOREIGN KEY(gateway_id,batch_id,source_id,stream_id,capture_generation,
            capture_seq,content_sha256) REFERENCES public.ingest_batches(gateway_id,
            batch_id,source_id,stream_id,capture_generation,capture_seq,content_sha256)
            ON DELETE RESTRICT',fk);
    END $$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.immutable_capture_clock_evidence()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$ BEGIN
            RAISE EXCEPTION 'Capture clock evidence is immutable';
        END $$""")
    op.execute("ALTER FUNCTION public.immutable_capture_clock_evidence() RESET ALL")
    op.execute("DROP TRIGGER capture_ingest_receipt ON public.ingest_batches")
    op.execute("DROP TRIGGER ingest_batch_retention_guard ON public.ingest_batches")
    op.execute("DROP TRIGGER ingest_batch_retention_no_truncate ON public.ingest_batches")
    op.execute("DROP FUNCTION public.guard_retired_ingest_batch()")
    op.execute("DROP TRIGGER consumed_clock_probe_guard ON public.capture_clock_probes")
    op.execute("DROP FUNCTION public.guard_consumed_clock_probe()")
    for function in (
        "prune_ingest_batches(integer,integer,integer)",
        "prune_expired_clock_probes(timestamptz,text,text,text,numeric,text)",
        "retention_hash_add(text,text)",
        "retention_parameters_match(jsonb,jsonb,text)",
        "retention_canonical_json(jsonb)",
        "capture_ingest_receipt()",
    ):
        op.execute(f"DROP FUNCTION public.{function}")
    for table in ("ingest_retention_state", "ingest_retention_ranges", "ingest_receipts"):
        op.drop_table(table)
    op.execute("DROP FUNCTION public.guard_ingest_retention()")
    op.drop_index("ix_ingest_retention_candidates", table_name="ingest_batches")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hmi_app') THEN
        GRANT DELETE ON public.capture_clock_probes TO hmi_app;
    END IF; END $$""")
